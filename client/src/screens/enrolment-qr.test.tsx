import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { QRCodeSVG } from "qrcode.react";

/**
 * The authenticator QR renders, and renders something scannable.
 *
 * `qrcode.react` is the one dependency whose React peer range had to be
 * widened by hand for React 19 -- 4.1.0 declared `^16 || ^17 || ^18` and would
 * have refused to install beside it. A peer range is a claim about
 * compatibility, not a proof of it, so this renders the component and looks at
 * what came out.
 *
 * It earns its place because of *where* this component lives: the setup wizard
 * and the invitation acceptance screen. Both are reachable only on a fresh
 * instance or with a one-time link, so neither is on any path somebody walks
 * while working on something else. A blank QR here is found by the person
 * trying to enrol their authenticator, at the one moment they cannot get past
 * it -- the wizard will not finish without a code that verifies.
 */
describe("the authenticator QR", () => {
  const uri = "otpauth://totp/Spend%20Tracker:demo@example.com?secret=JBSWY3DPEHPK3PXP&issuer=Spend%20Tracker";

  it("renders an svg carrying the modules, not an empty element", () => {
    const html = renderToStaticMarkup(<QRCodeSVG value={uri} size={160} />);

    expect(html.startsWith("<svg")).toBe(true);
    // The dark modules are drawn as path data. An empty <svg>, which is what a
    // silently broken render gives, has none -- so asserting the element alone
    // would pass on exactly the failure this is here to catch.
    expect(html).toContain("<path");
    expect(html).toMatch(/d="[^"]{100,}"/);
  });

  it("encodes a different secret differently", () => {
    // Otherwise a component that ignored its input and drew a fixed placeholder
    // would satisfy the test above.
    const one = renderToStaticMarkup(<QRCodeSVG value={uri} size={160} />);
    const two = renderToStaticMarkup(
      <QRCodeSVG value={uri.replace("JBSWY3DPEHPK3PXP", "KRSXG5CTMVRXEZLU")} size={160} />,
    );
    expect(one).not.toEqual(two);
  });
});
