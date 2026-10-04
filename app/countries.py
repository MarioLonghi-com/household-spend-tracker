"""The country list, and why it is two letters.

**ISO 3166-1 alpha-2** is what "the standard countries" means, and picking it
buys two things at once. It is the canonical list, and it is also what a flag
emoji *is*: 🇪🇸 is not a picture of a flag, it is the two letters `ES` written
as regional indicator symbols, and any two-letter code renders as whatever flag
the reader's system has for it. So storing the code gives the flag for free --
no image files, no emoji lookup table, nothing to keep in step with the names.

The fallback is 🇺🇳, which works for the same reason: `UN` is an exceptionally
reserved code in the same standard, so the United Nations flag is just `U` and
`N` written the same way. A stored value that is not a code we know, or no
value at all, shows it.

The list is the 249 officially assigned alpha-2 codes. It changes roughly once
a decade -- South Sudan in 2011 was the last addition -- which is why it is
data in the repository rather than a dependency: a package that updates twice a
year for a list that changes twice a century is a supply chain for nothing.
"""

from __future__ import annotations

from .errors import ValidationError

#: The UN flag, shown when a country is unknown or unset. Not a country.
UNKNOWN = "UN"

#: `(alpha-2, name)`, ISO 3166-1, in English short-name order.
COUNTRIES: tuple[tuple[str, str], ...] = (
    ("AF", "Afghanistan"), ("AX", "Åland Islands"), ("AL", "Albania"), ("DZ", "Algeria"),
    ("AS", "American Samoa"), ("AD", "Andorra"), ("AO", "Angola"), ("AI", "Anguilla"),
    ("AQ", "Antarctica"), ("AG", "Antigua and Barbuda"), ("AR", "Argentina"), ("AM", "Armenia"),
    ("AW", "Aruba"), ("AU", "Australia"), ("AT", "Austria"), ("AZ", "Azerbaijan"),
    ("BS", "Bahamas"), ("BH", "Bahrain"), ("BD", "Bangladesh"), ("BB", "Barbados"),
    ("BY", "Belarus"), ("BE", "Belgium"), ("BZ", "Belize"), ("BJ", "Benin"),
    ("BM", "Bermuda"), ("BT", "Bhutan"), ("BO", "Bolivia"), ("BQ", "Bonaire, Sint Eustatius and Saba"),
    ("BA", "Bosnia and Herzegovina"), ("BW", "Botswana"), ("BV", "Bouvet Island"), ("BR", "Brazil"),
    ("IO", "British Indian Ocean Territory"), ("BN", "Brunei Darussalam"), ("BG", "Bulgaria"),
    ("BF", "Burkina Faso"), ("BI", "Burundi"), ("CV", "Cabo Verde"), ("KH", "Cambodia"),
    ("CM", "Cameroon"), ("CA", "Canada"), ("KY", "Cayman Islands"), ("CF", "Central African Republic"),
    ("TD", "Chad"), ("CL", "Chile"), ("CN", "China"), ("CX", "Christmas Island"),
    ("CC", "Cocos (Keeling) Islands"), ("CO", "Colombia"), ("KM", "Comoros"), ("CG", "Congo"),
    ("CD", "Congo, Democratic Republic of the"), ("CK", "Cook Islands"), ("CR", "Costa Rica"),
    ("CI", "Côte d'Ivoire"), ("HR", "Croatia"), ("CU", "Cuba"), ("CW", "Curaçao"),
    ("CY", "Cyprus"), ("CZ", "Czechia"), ("DK", "Denmark"), ("DJ", "Djibouti"),
    ("DM", "Dominica"), ("DO", "Dominican Republic"), ("EC", "Ecuador"), ("EG", "Egypt"),
    ("SV", "El Salvador"), ("GQ", "Equatorial Guinea"), ("ER", "Eritrea"), ("EE", "Estonia"),
    ("SZ", "Eswatini"), ("ET", "Ethiopia"), ("FK", "Falkland Islands"), ("FO", "Faroe Islands"),
    ("FJ", "Fiji"), ("FI", "Finland"), ("FR", "France"), ("GF", "French Guiana"),
    ("PF", "French Polynesia"), ("TF", "French Southern Territories"), ("GA", "Gabon"),
    ("GM", "Gambia"), ("GE", "Georgia"), ("DE", "Germany"), ("GH", "Ghana"),
    ("GI", "Gibraltar"), ("GR", "Greece"), ("GL", "Greenland"), ("GD", "Grenada"),
    ("GP", "Guadeloupe"), ("GU", "Guam"), ("GT", "Guatemala"), ("GG", "Guernsey"),
    ("GN", "Guinea"), ("GW", "Guinea-Bissau"), ("GY", "Guyana"), ("HT", "Haiti"),
    ("HM", "Heard Island and McDonald Islands"), ("VA", "Holy See"), ("HN", "Honduras"),
    ("HK", "Hong Kong"), ("HU", "Hungary"), ("IS", "Iceland"), ("IN", "India"),
    ("ID", "Indonesia"), ("IR", "Iran"), ("IQ", "Iraq"), ("IE", "Ireland"),
    ("IM", "Isle of Man"), ("IL", "Israel"), ("IT", "Italy"), ("JM", "Jamaica"),
    ("JP", "Japan"), ("JE", "Jersey"), ("JO", "Jordan"), ("KZ", "Kazakhstan"),
    ("KE", "Kenya"), ("KI", "Kiribati"), ("KP", "Korea, Democratic People's Republic of"),
    ("KR", "Korea, Republic of"), ("KW", "Kuwait"), ("KG", "Kyrgyzstan"),
    ("LA", "Lao People's Democratic Republic"), ("LV", "Latvia"), ("LB", "Lebanon"),
    ("LS", "Lesotho"), ("LR", "Liberia"), ("LY", "Libya"), ("LI", "Liechtenstein"),
    ("LT", "Lithuania"), ("LU", "Luxembourg"), ("MO", "Macao"), ("MG", "Madagascar"),
    ("MW", "Malawi"), ("MY", "Malaysia"), ("MV", "Maldives"), ("ML", "Mali"),
    ("MT", "Malta"), ("MH", "Marshall Islands"), ("MQ", "Martinique"), ("MR", "Mauritania"),
    ("MU", "Mauritius"), ("YT", "Mayotte"), ("MX", "Mexico"), ("FM", "Micronesia"),
    ("MD", "Moldova"), ("MC", "Monaco"), ("MN", "Mongolia"), ("ME", "Montenegro"),
    ("MS", "Montserrat"), ("MA", "Morocco"), ("MZ", "Mozambique"), ("MM", "Myanmar"),
    ("NA", "Namibia"), ("NR", "Nauru"), ("NP", "Nepal"), ("NL", "Netherlands"),
    ("NC", "New Caledonia"), ("NZ", "New Zealand"), ("NI", "Nicaragua"), ("NE", "Niger"),
    ("NG", "Nigeria"), ("NU", "Niue"), ("NF", "Norfolk Island"), ("MK", "North Macedonia"),
    ("MP", "Northern Mariana Islands"), ("NO", "Norway"), ("OM", "Oman"), ("PK", "Pakistan"),
    ("PW", "Palau"), ("PS", "Palestine, State of"), ("PA", "Panama"), ("PG", "Papua New Guinea"),
    ("PY", "Paraguay"), ("PE", "Peru"), ("PH", "Philippines"), ("PN", "Pitcairn"),
    ("PL", "Poland"), ("PT", "Portugal"), ("PR", "Puerto Rico"), ("QA", "Qatar"),
    ("RE", "Réunion"), ("RO", "Romania"), ("RU", "Russian Federation"), ("RW", "Rwanda"),
    ("BL", "Saint Barthélemy"), ("SH", "Saint Helena, Ascension and Tristan da Cunha"),
    ("KN", "Saint Kitts and Nevis"), ("LC", "Saint Lucia"), ("MF", "Saint Martin (French part)"),
    ("PM", "Saint Pierre and Miquelon"), ("VC", "Saint Vincent and the Grenadines"),
    ("WS", "Samoa"), ("SM", "San Marino"), ("ST", "Sao Tome and Principe"), ("SA", "Saudi Arabia"),
    ("SN", "Senegal"), ("RS", "Serbia"), ("SC", "Seychelles"), ("SL", "Sierra Leone"),
    ("SG", "Singapore"), ("SX", "Sint Maarten (Dutch part)"), ("SK", "Slovakia"),
    ("SI", "Slovenia"), ("SB", "Solomon Islands"), ("SO", "Somalia"), ("ZA", "South Africa"),
    ("GS", "South Georgia and the South Sandwich Islands"), ("SS", "South Sudan"),
    ("ES", "Spain"), ("LK", "Sri Lanka"), ("SD", "Sudan"), ("SR", "Suriname"),
    ("SJ", "Svalbard and Jan Mayen"), ("SE", "Sweden"), ("CH", "Switzerland"),
    ("SY", "Syrian Arab Republic"), ("TW", "Taiwan"), ("TJ", "Tajikistan"), ("TZ", "Tanzania"),
    ("TH", "Thailand"), ("TL", "Timor-Leste"), ("TG", "Togo"), ("TK", "Tokelau"),
    ("TO", "Tonga"), ("TT", "Trinidad and Tobago"), ("TN", "Tunisia"), ("TR", "Türkiye"),
    ("TM", "Turkmenistan"), ("TC", "Turks and Caicos Islands"), ("TV", "Tuvalu"),
    ("UG", "Uganda"), ("UA", "Ukraine"), ("AE", "United Arab Emirates"), ("GB", "United Kingdom"),
    ("US", "United States of America"), ("UM", "United States Minor Outlying Islands"),
    ("UY", "Uruguay"), ("UZ", "Uzbekistan"), ("VU", "Vanuatu"), ("VE", "Venezuela"),
    ("VN", "Viet Nam"), ("VG", "Virgin Islands (British)"), ("VI", "Virgin Islands (U.S.)"),
    ("WF", "Wallis and Futuna"), ("EH", "Western Sahara"), ("YE", "Yemen"), ("ZM", "Zambia"),
    ("ZW", "Zimbabwe"),
)

BY_CODE: dict[str, str] = dict(COUNTRIES)


def flag(code: str | None) -> str:
    """The flag for a code, or the UN's when there is not one.

    Two regional indicator symbols, which is all a flag emoji is. Nothing is
    looked up: `A` is U+1F1E6 and the rest follow, so any pair of letters
    renders as whatever the reader's system has for it.
    """
    letters = (code or "").strip().upper()
    if letters not in BY_CODE:
        letters = UNKNOWN
    return "".join(chr(0x1F1E6 + ord(letter) - ord("A")) for letter in letters)


def check(code: str | None) -> str | None:
    """Tidy a country code, or refuse it. `None` stays `None` -- it is a real
    answer, and most accounts will never say."""
    if code is None or not code.strip():
        return None
    letters = code.strip().upper()
    if letters not in BY_CODE:
        raise ValidationError(f"{code!r} is not a country code")
    return letters


#: How a bank in each country writes the decimal point, for a statement whose
#: amounts cannot say it themselves: a Spanish export of whole thousands is
#: "1.500" all the way down, which reads as one and a half with "." (#259).
#: Only consulted then -- one amount in the file that settles it always wins.
#:
#: Deliberately short. A country is here only when its banks write it one way;
#: anywhere unlisted, or split -- Canada (Quebec writes a comma), South Africa
#: (the standard says comma, many banks write a point) -- has no preference,
#: and the import says the separator was assumed.
DECIMAL_SEPARATORS: dict[str, str] = {
    **dict.fromkeys(
        (
            # Continental European countries whose banks write a comma. Not
            # "the euro area": Cyprus and Malta use the euro and write a point.
            "AD", "AT", "BE", "BG", "CZ", "DE", "DK", "EE", "ES", "FI", "FR", "GR",
            "HR", "HU", "IS", "IT", "LT", "LU", "LV", "MC", "NL", "NO", "PL", "PT",
            "RO", "RS", "RU", "SE", "SI", "SK", "SM", "TR", "UA",
            # South America, and two in Asia.
            "AR", "BR", "CL", "CO", "UY", "ID", "VN",
        ),
        ",",
    ),
    **dict.fromkeys(
        (
            "GB", "IE", "CY", "MT", "US", "AU", "NZ", "CH", "MX", "IN", "JP", "CN",
            "HK", "SG", "KR", "IL", "TH", "MY", "PH",
        ),
        ".",
    ),
}


def decimal_separator(code: str | None) -> str | None:
    """The separator a bank in this country writes, or None if that is not settled."""
    return DECIMAL_SEPARATORS.get((code or "").strip().upper())
