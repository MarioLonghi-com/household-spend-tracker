"""The mobile capture page, and the three optional fields it sends.

Tests 21 to 24. The most valuable one is the first: the whole reason `/snap`
sends three fields instead of one is that compressing on the phone the obvious
way destroys the metadata *and* the dedupe key, and both failures are silent.
"""

from __future__ import annotations

import base64
import hashlib
import io

import pytest
from PIL import Image

from tests.conftest import HEADERS, _setup_owner

from .receipt_fixtures import as_bytes, receipt_image, with_exif


def _house(client) -> str:
    _setup_owner(client)
    return client.post("/api/households", json={"name": "Ours"}, headers=HEADERS).json()["id"]


def _app1(jpeg: bytes) -> bytes:
    """The APP1 segment, read the way `snap.js` reads it."""
    at = 2
    while at + 4 < len(jpeg):
        assert jpeg[at] == 0xFF
        marker = jpeg[at + 1]
        length = (jpeg[at + 2] << 8) | jpeg[at + 3]
        if marker == 0xE1:
            return jpeg[at + 4 : at + 2 + length]
        if marker == 0xDA:
            break
        at += 2 + length
    raise AssertionError("the fixture has no APP1 block, so this test proves nothing")


def _phone(original: bytes) -> tuple[bytes, str, str]:
    """What the page actually sends: a smaller JPEG, the original's hash, its EXIF.

    The re-encode here stands in for `canvas.toBlob` -- and, exactly like the
    canvas, it carries no metadata across at all. That is the point.
    """
    with Image.open(io.BytesIO(original)) as image:
        image.thumbnail((2000, 2000), Image.LANCZOS)
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=90)
    return (
        buffer.getvalue(),
        hashlib.sha256(original).hexdigest(),
        base64.b64encode(_app1(original)).decode(),
    )


def _upload(client, household_id, raw, **fields):
    return client.post(
        f"/api/households/{household_id}/receipts",
        files={"file": ("photo.jpg", raw, "image/jpeg")},
        data={key: str(value) for key, value in fields.items() if value is not None},
        headers=HEADERS,
    )


# --------------------------------------------------------------------------- #
# The three fields
# --------------------------------------------------------------------------- #


def test_a_compressed_upload_keeps_the_original_hash_and_the_metadata(client):
    """Test 21.

    The re-encoded bytes carry no EXIF -- asserted, because if they did this
    test would pass for the wrong reason -- and the stored receipt still holds
    the camera, the capture time and the coordinates, and is keyed by the hash
    of what the camera wrote.
    """
    house = _house(client)
    original = with_exif(receipt_image((2400, 3200)))
    smaller, sha, exif = _phone(original)

    with Image.open(io.BytesIO(smaller)) as check:
        assert not check.getexif(), "the fixture must lose its EXIF, or this proves nothing"
    assert len(smaller) < len(original)

    made = _upload(
        client, house, smaller, original_sha256=sha, exif=exif, client_encoded=True
    )
    assert made.status_code == 201, made.text
    receipt = made.json()["receipt"]

    assert receipt["content_sha256"] == sha, (
        "keyed by the original, or the same photo from the phone and the desktop "
        "becomes two receipts"
    )
    assert receipt["camera"] == "Fictional Handset 9"
    assert receipt["captured_at"] is not None
    assert receipt["gps_lat"] == pytest.approx(40.4215, abs=1e-3)
    assert receipt["client_encoded"] is True, "a receipt through two encoders says so"


def test_the_same_photo_from_the_phone_and_the_desktop_is_one_receipt(client):
    """Test 22, and the spec's version of it asked for something impossible.

    It wanted **one blob row**, on the reasoning that both doors lead to the
    same photograph. They do not lead to the same *bytes*: the phone sends a
    2000px JPEG it made and the desktop sends the original, and storing one
    under the other's hash is exactly the cross-household read `blob_sha256`
    exists to close. The two requirements cannot both hold, and the spec did
    not notice.

    What matters is the thing that was actually asked for, and it does hold:
    the same photograph is **one identity**, so the second upload is
    recognised rather than becoming an unrelated receipt. The storage saving
    was never the point of it, and still applies wherever the bytes really are
    identical -- see the test below.
    """
    house = _house(client)
    original = with_exif(receipt_image((2000, 2600)))
    smaller, sha, exif = _phone(original)

    from_phone = _upload(client, house, smaller, original_sha256=sha, exif=exif,
                         client_encoded=True)
    assert from_phone.status_code == 201
    assert from_phone.json()["receipt"]["content_sha256"] == sha

    # The same photograph off the desktop, uncompressed and byte-different --
    # and refused, because it is the same photograph. That refusal *is* the
    # proof: without the three fields the server would hash the phone's
    # re-encode, the two hashes would differ, and this would sail through as
    # an unrelated receipt.
    from_desktop = _upload(client, house, original)
    assert from_desktop.status_code == 409, (
        "one photograph, one identity, whichever door it came in"
    )
    assert "already waiting in the inbox" in from_desktop.json()["detail"]

    # And on a transaction, the same way round.
    account = client.post(
        f"/api/households/{house}/accounts",
        json={"name": "Checking", "type": "checking"},
        headers=HEADERS,
    ).json()
    txn = client.post(
        f"/api/households/{house}/transactions",
        json={"account_id": account["id"], "date": "2026-09-19", "amount": -1234},
        headers=HEADERS,
    ).json()
    assert _upload(client, house, original, transaction_id=txn["id"]).status_code == 201
    again = _upload(client, house, smaller, original_sha256=sha, exif=exif,
                    transaction_id=txn["id"])
    assert again.status_code == 409


def test_byte_identical_uploads_really_do_share_one_blob(client):
    """The storage claim, held to what is actually true.

    Identical bytes are stored once however many receipts point at them --
    the desktop-to-desktop case and the two-households-one-PDF case, which is
    what content-addressing buys and all it buys.
    """
    from sqlalchemy import func, select

    from app.models import ReceiptBlob

    house = _house(client)
    raw = with_exif(receipt_image((900, 1200)))
    account = client.post(
        f"/api/households/{house}/accounts",
        json={"name": "Checking", "type": "checking"},
        headers=HEADERS,
    ).json()
    txn = client.post(
        f"/api/households/{house}/transactions",
        json={"account_id": account["id"], "date": "2026-09-19", "amount": -999},
        headers=HEADERS,
    ).json()

    # One in the inbox and one on a row: two receipts, legitimately, because a
    # bill can be evidence for a transaction while a copy waits to be matched.
    assert _upload(client, house, raw).status_code == 201
    assert _upload(client, house, raw, transaction_id=txn["id"]).status_code == 201

    with client.app_module.session_scope() as session:
        blobs = session.execute(
            select(func.count(func.distinct(ReceiptBlob.sha256)))
        ).scalar_one()
    assert blobs == 1


def test_a_client_that_sends_nothing_extra_is_the_ordinary_case(client):
    """The desktop path, and it must stay the path with no special casing."""
    house = _house(client)
    raw = with_exif()
    made = _upload(client, house, raw)

    assert made.status_code == 201
    receipt = made.json()["receipt"]
    assert receipt["content_sha256"] == hashlib.sha256(raw).hexdigest()
    assert receipt["client_encoded"] is False
    assert receipt["camera"] == "Fictional Handset 9", "parsed from what we received"


def test_a_lie_about_the_hash_cannot_read_somebody_elses_receipt(client):
    """The property that makes accepting the field safe, pinned.

    A wrong `original_sha256` can create a duplicate or block one of your own
    uploads, and can do nothing else. The first implementation of this did not
    hold: `content_sha256` was both the dedupe key *and* the pointer into the
    blob store, so claiming somebody else's hash pointed the new receipt at
    their picture, and the app served it back. This test is what found it, and
    `blob_sha256` is what fixes it.
    """
    house = _house(client)
    account = client.post(
        f"/api/households/{house}/accounts",
        json={"name": "Checking", "type": "checking"},
        headers=HEADERS,
    ).json()
    txn = client.post(
        f"/api/households/{house}/transactions",
        json={"account_id": account["id"], "date": "2026-09-19", "amount": -100},
        headers=HEADERS,
    ).json()

    mine = _upload(client, house, as_bytes(receipt_image((400, 500)), "PNG"))
    assert mine.status_code == 201
    real_sha = mine.json()["receipt"]["content_sha256"]

    # Different bytes, claiming the other receipt's identity. Onto a
    # transaction, because the inbox already holds that identity and would
    # refuse it before the interesting part.
    other = as_bytes(receipt_image((300, 400)), "JPEG")
    lied = _upload(client, house, other, original_sha256=real_sha, transaction_id=txn["id"])
    assert lied.status_code == 201

    served = client.get(f"/api/receipts/{lied.json()['receipt']['id']}/display")
    with Image.open(io.BytesIO(served.content)) as image:
        assert image.size == (300, 400), (
            "the hash decided what to deduplicate against, not what to serve"
        )


# --------------------------------------------------------------------------- #
# The page itself
# --------------------------------------------------------------------------- #


def test_the_page_has_no_inline_script(client):
    """Test 24, first half.

    `script-src 'self'` with no 'unsafe-inline' means a single-file page
    renders blank with nothing but a console violation -- on a phone, at a
    till, which nobody is reading. This test exists because that failure is
    invisible and the fix looks like a regression in tidiness.
    """
    _setup_owner(client)
    page = client.get("/snap")
    assert page.status_code == 200
    body = page.text

    import re

    inline = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", body, re.S)
    assert all(not one.strip() for one in inline), (
        "an inline <script> here never runs, and nothing on screen says so"
    )
    assert '<script type="module" src="/snap/snap.js">' in body

    script = client.get("/snap/snap.js")
    assert script.status_code == 200
    assert "createImageBitmap" in script.text


def test_signed_out_it_redirects_rather_than_refusing(client):
    """Test 24, second half.

    `current_user` would answer 401 with a JSON body, which on a phone is a
    blank screen -- and a redirect to the register would be a lost receipt,
    because the person is standing at a till holding a piece of paper.
    """
    _setup_owner(client)
    client.delete("/api/session", headers=HEADERS)

    refused = client.get("/snap", follow_redirects=False)
    assert refused.status_code == 303
    assert refused.headers["location"] == "/?next=%2Fsnap"


def test_the_page_can_install_to_a_home_screen(client):
    """Which is the difference between a feature used monthly and one used at
    the till, and it costs four keys."""
    _setup_owner(client)
    manifest = client.get("/snap/manifest.json").json()
    assert manifest["start_url"] == "/snap"
    assert manifest["display"] == "standalone"


# --------------------------------------------------------------------------- #
# The location toggle
# --------------------------------------------------------------------------- #


def test_a_device_fix_fills_in_where_the_photo_carried_none(client):
    """The toggle's whole reason to exist, and the rule that keeps it honest.

    A photo that lost its GPS -- a share sheet, an Android browser that strips
    it, a scan -- gets the phone's own position, in the same columns, stamped
    with where it came from so the panel can say so.
    """
    house = _house(client)
    stripped = with_exif(gps=False)

    made = _upload(
        client,
        house,
        stripped,
        device_lat=48.8584,
        device_lon=2.2945,
        device_accuracy_m=18.5,
    )
    assert made.status_code == 201, made.text
    receipt = made.json()["receipt"]

    assert receipt["gps_lat"] == pytest.approx(48.8584)
    assert receipt["gps_lon"] == pytest.approx(2.2945)
    assert receipt["gps_accuracy_m"] == pytest.approx(18.5)
    assert receipt["gps_bearing"] is None, (
        "a browser fix knows where the phone is and nothing about which way it faced"
    )
    assert receipt["exif"]["SpendTrackerLocationSource"] == "device", (
        "without this the register has two notions of where a receipt was taken "
        "and no way to tell them apart"
    )
    # The rest of the metadata is untouched: this fills a gap, it does not
    # replace the parse.
    assert receipt["camera"] == "Fictional Handset 9"
    assert receipt["captured_at"] is not None


def test_the_cameras_own_fix_beats_the_devices(client):
    """EXIF is about the photograph; the browser is about the upload.

    They are the same thing at a till and a completely different thing when a
    wallet is emptied on the kitchen table on Sunday, so the stronger claim
    wins and the weaker one is discarded rather than stored beside it.
    """
    house = _house(client)

    made = _upload(
        client,
        house,
        with_exif(),  # Madrid
        device_lat=48.8584,  # Paris
        device_lon=2.2945,
        device_accuracy_m=18.5,
    )
    assert made.status_code == 201, made.text
    receipt = made.json()["receipt"]

    assert receipt["gps_lat"] == pytest.approx(40.4215, abs=1e-3)
    assert receipt["gps_lon"] == pytest.approx(-3.6889, abs=1e-3)
    assert "SpendTrackerLocationSource" not in (receipt["exif"] or {}), (
        "the coordinate came from the camera, and the row must not claim otherwise"
    )


def test_a_device_fix_that_is_not_a_place_on_earth_is_refused(client):
    """Three attacker-supplied floats that end up in a map link.

    A 400 with a sentence, never a 500 and never a row the UI cannot draw.
    """
    house = _house(client)
    raw = with_exif(gps=False)

    for lat, lon in ((900.0, 2.0), (48.0, -1000.0), (float("nan"), 2.0)):
        refused = _upload(client, house, raw, device_lat=lat, device_lon=lon)
        assert refused.status_code in (400, 422), refused.text

    # Half a fix is a column pair meaning "somewhere on this line of latitude".
    half = _upload(client, house, raw, device_lat=48.8584)
    assert half.status_code in (400, 422), half.text

    assert client.get(f"/api/households/{house}/receipts").json() == [], (
        "a refused fix must not leave the photograph stored without it"
    )

    # And the same upload with no location at all is the ordinary case.
    assert _upload(client, house, raw).status_code == 201


def test_the_location_toggle_ships_switched_off(client):
    """The privacy posture, pinned in the markup that is actually served.

    It does not prove the script never asks before the toggle is pressed --
    only reading `snap.js` does that. It does prove that the control the phone
    renders starts off, which is the half a refactor can flip by accident.
    """
    _setup_owner(client)
    page = client.get("/snap")
    assert page.status_code == 200
    body = page.text

    import re

    toggle = re.search(r'<button[^>]*id="gps"[^>]*>', body)
    assert toggle is not None, "there is no location toggle on the page"
    assert 'aria-pressed="false"' in toggle.group(0), (
        "the toggle is off by default, and the browser is asked nothing until it is on"
    )
    assert "where this phone is" in body, (
        "the page has to say what it is about to record before it records it"
    )

    script = client.get("/snap/snap.js").text
    assert "getCurrentPosition" in script
    assert "device_lat" in script and "device_lon" in script


def test_the_location_toggle_checks_the_secure_context_before_it_asks(client):
    """The reported bug: the toggle "doesn't trigger permission request".

    Geolocation is a powerful feature, so a browser only offers it on a secure
    context -- HTTPS, or localhost. `/snap` on a phone is reached at
    `http://<lan-ip>:8848`, where `navigator.geolocation` still exists and
    `getCurrentPosition` still runs: nothing prompts, and the failure arrives as
    code 1, which is the same code a person tapping "Don't allow" produces. The
    page then blamed a `Permissions-Policy` it had already stopped sending.

    Read out of the served script rather than out of the file on disk, because
    what a phone runs is what came down the wire. The header the page needs is
    asserted in `test_security_headers.py`; this is the other half.
    """
    _setup_owner(client)
    script = client.get("/snap/snap.js")
    assert script.status_code == 200
    source = script.text

    assert "isSecureContext" in source, (
        "the toggle has to know it is on a plain-http origin before it calls, "
        "because afterwards it cannot tell that case from a refusal"
    )
    # And it is checked ahead of the call, not used to explain one afterwards.
    # Compared against the call itself rather than against the word, which
    # appears in the comment above it that explains why this exists.
    assert "function blocked()" in source
    assert source.index("function blocked()") < source.index(
        "navigator.geolocation.getCurrentPosition("
    )
    asking = source.index("navigator.geolocation.getCurrentPosition(")
    assert "const cannot = blocked();" in source[:asking], (
        "`position()` calls `blocked()` on the way in, so a caller that never "
        "went through the toggle is refused for the real reason too"
    )
    assert "Permissions-Policy that switches geolocation off" not in source, (
        "that sentence named a cause that was fixed when the toggle was built, "
        "and it was the sentence people read"
    )

    page = client.get("/snap").text
    assert 'aria-describedby="gpswhy"' in page, (
        "the reason lives in the paragraph the button points at, so a screen "
        "reader gets it as part of the control rather than as loose text"
    )


def test_snap_and_the_app_agree_on_where_the_appearance_choice_lives():
    """One key, written by the app and read by the capture page.

    `/snap` is plain HTML with no bundler, so it cannot import
    `client/src/lib/appearance.ts` and holds its own copy of the key. A copy
    that drifts does not crash anything -- the page simply stops honouring a
    choice the person made, on the one screen most likely to be used in the
    dark, and nothing says so.
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    module = (root / "client" / "src" / "lib" / "appearance.ts").read_text()
    script = (root / "app" / "static" / "snap" / "snap.js").read_text()

    in_app = re.search(r'APPEARANCE_KEY\s*=\s*"([^"]+)"', module)
    in_snap = re.search(r'APPEARANCE_KEY\s*=\s*"([^"]+)"', script)
    assert in_app, "appearance.ts no longer declares APPEARANCE_KEY the way this test reads it"
    assert in_snap, "snap.js no longer declares APPEARANCE_KEY the way this test reads it"
    assert in_app.group(1) == in_snap.group(1) == "spendtracker.appearance"


def test_the_capture_page_can_be_held_in_one_scheme():
    """The stylesheet has to carry both halves of the override.

    Without the `:not()` a person who chose light still gets dark from the
    media query on a dark phone; without the attribute rule, choosing dark on a
    light phone does nothing.
    """
    from pathlib import Path

    page = (Path(__file__).resolve().parent.parent / "app" / "static" / "snap" / "index.html").read_text()
    assert ':root:not([data-theme="light"])' in page
    assert ':root[data-theme="dark"]' in page
    # And the browser's own furniture is told, or a forced theme leaves white
    # select menus on a dark page.
    assert ':root[data-theme="dark"] { color-scheme: dark; }' in page


# Reads theme.ts as well as snap.js: run on every pull request, so a change to
# either alone still meets this.
@pytest.mark.repo_wide
def test_snap_paints_the_accent_only_when_it_is_hex():
    """#92: `/snap` set the header's background from the server unchecked.

    The SPA's `theme.ts` re-checks every colour; `/snap` has no bundler and
    cannot import it, so it carries its own copy of the regex. The copy is
    held to the app's, and `accentOf` itself is run on good and bad values.
    """
    import json
    import re
    import shutil
    import subprocess
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    theme = (root / "client" / "src" / "lib" / "theme.ts").read_text()
    script = (root / "app" / "static" / "snap" / "snap.js").read_text()

    in_app = re.search(r"^const HEX = (/.+/);$", theme, re.M)
    in_snap = re.search(r"^const HEX = (/.+/);$", script, re.M)
    assert in_app and in_snap, "HEX is not declared where this test reads it"
    assert in_app.group(1) == in_snap.group(1)

    accent_of = re.search(r"^function accentOf\(house\) \{\n.*?^\}$", script, re.M | re.S)
    assert accent_of, "accentOf is not where this test reads it"
    assert re.search(r"style\.background = accent;", script)
    assert len(re.findall(r"style\.background", script)) == 1, "one place paints the header"

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is missing")
    harness = "\n".join(
        [
            'function schemeNow() { return "light"; }',
            in_snap.group(0),
            accent_of.group(0),
            "const cases = JSON.parse(process.argv[1]);",
            "console.log(JSON.stringify(cases.map((c) => accentOf({ colours: { light: { accent: c } } }))));",
        ]
    )
    cases = ["#1a2b3c", "#ABCDEF", "red", "#12345", "#1234567", "#12345g", "red; } body { color: red", 12, None]
    done = subprocess.run(
        [node, "-e", harness, json.dumps(cases)], capture_output=True, text=True, timeout=60
    )
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == ["#1a2b3c", "#ABCDEF", None, None, None, None, None, None, None]
