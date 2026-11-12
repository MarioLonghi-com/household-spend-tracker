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


# --------------------------------------------------------------------------- #
# What the first-run wizard suggests for a country (#458)
# --------------------------------------------------------------------------- #

#: Countries sharing a currency, by ISO 4217 code. Every other country has its
#: own, in `_OWN_CURRENCY`. Antarctica has none and is suggested nothing.
_SHARED_CURRENCY: dict[str, str] = {
    **dict.fromkeys(
        (
            "AD", "AT", "AX", "BE", "BL", "CY", "DE", "EE", "ES", "FI", "FR", "GF",
            "GP", "GR", "HR", "IE", "IT", "LT", "LU", "LV", "MC", "ME", "MF", "MQ",
            "MT", "NL", "PM", "PT", "RE", "SI", "SK", "SM", "TF", "VA", "YT",
        ),
        "EUR",
    ),
    **dict.fromkeys(
        (
            "AS", "BQ", "EC", "FM", "GU", "IO", "MH", "MP", "PR", "PW", "SV", "TC",
            "TL", "UM", "US", "VG", "VI",
        ),
        "USD",
    ),
    **dict.fromkeys(("BJ", "BF", "CI", "GW", "ML", "NE", "SN", "TG"), "XOF"),
    **dict.fromkeys(("CM", "CF", "TD", "CG", "GQ", "GA"), "XAF"),
    **dict.fromkeys(("AG", "AI", "DM", "GD", "MS", "KN", "LC", "VC"), "XCD"),
    **dict.fromkeys(("NC", "PF", "WF"), "XPF"),
    **dict.fromkeys(("AU", "CX", "CC", "HM", "KI", "NR", "NF", "TV"), "AUD"),
    **dict.fromkeys(("NZ", "CK", "NU", "PN", "TK"), "NZD"),
    **dict.fromkeys(("NO", "SJ", "BV"), "NOK"),
    **dict.fromkeys(("DK", "FO", "GL"), "DKK"),
    **dict.fromkeys(("CH", "LI"), "CHF"),
    **dict.fromkeys(("GB", "GG", "IM", "JE", "GS"), "GBP"),
    **dict.fromkeys(("MA", "EH"), "MAD"),
    **dict.fromkeys(("IL", "PS"), "ILS"),
    **dict.fromkeys(("CW", "SX"), "XCG"),
}

#: A country whose currency is its own, by code.
_OWN_CURRENCY: dict[str, str] = {
    "AE": "AED", "AF": "AFN", "AL": "ALL", "AM": "AMD", "AO": "AOA", "AR": "ARS",
    "AW": "AWG", "AZ": "AZN", "BA": "BAM", "BB": "BBD", "BD": "BDT", "BG": "BGN",
    "BH": "BHD", "BI": "BIF", "BM": "BMD", "BN": "BND", "BO": "BOB", "BR": "BRL",
    "BS": "BSD", "BT": "BTN", "BW": "BWP", "BY": "BYN", "BZ": "BZD", "CA": "CAD",
    "CD": "CDF", "CL": "CLP", "CN": "CNY", "CO": "COP", "CR": "CRC", "CU": "CUP",
    "CV": "CVE", "CZ": "CZK", "DJ": "DJF", "DO": "DOP", "DZ": "DZD", "EG": "EGP",
    "ER": "ERN", "ET": "ETB", "FJ": "FJD", "FK": "FKP", "GE": "GEL", "GH": "GHS",
    "GI": "GIP", "GM": "GMD", "GN": "GNF", "GT": "GTQ", "GY": "GYD", "HK": "HKD",
    "HN": "HNL", "HT": "HTG", "HU": "HUF", "ID": "IDR", "IN": "INR", "IQ": "IQD",
    "IR": "IRR", "IS": "ISK", "JM": "JMD", "JO": "JOD", "JP": "JPY", "KE": "KES",
    "KG": "KGS", "KH": "KHR", "KM": "KMF", "KP": "KPW", "KR": "KRW", "KW": "KWD",
    "KY": "KYD", "KZ": "KZT", "LA": "LAK", "LB": "LBP", "LK": "LKR", "LR": "LRD",
    "LS": "LSL", "LY": "LYD", "MD": "MDL", "MG": "MGA", "MK": "MKD", "MM": "MMK",
    "MN": "MNT", "MO": "MOP", "MR": "MRU", "MU": "MUR", "MV": "MVR", "MW": "MWK",
    "MX": "MXN", "MY": "MYR", "MZ": "MZN", "NA": "NAD", "NG": "NGN", "NI": "NIO",
    "NP": "NPR", "OM": "OMR", "PA": "PAB", "PE": "PEN", "PG": "PGK", "PH": "PHP",
    "PK": "PKR", "PL": "PLN", "PY": "PYG", "QA": "QAR", "RO": "RON", "RS": "RSD",
    "RU": "RUB", "RW": "RWF", "SA": "SAR", "SB": "SBD", "SC": "SCR", "SD": "SDG",
    "SE": "SEK", "SG": "SGD", "SH": "SHP", "SL": "SLE", "SO": "SOS", "SR": "SRD",
    "SS": "SSP", "ST": "STN", "SY": "SYP", "SZ": "SZL", "TH": "THB", "TJ": "TJS",
    "TM": "TMT", "TN": "TND", "TO": "TOP", "TR": "TRY", "TT": "TTD", "TW": "TWD",
    "TZ": "TZS", "UA": "UAH", "UG": "UGX", "UY": "UYU", "UZ": "UZS", "VE": "VES",
    "VN": "VND", "VU": "VUV", "WS": "WST", "YE": "YER", "ZA": "ZAR", "ZM": "ZMW",
    "ZW": "ZWG",
}

CURRENCY: dict[str, str] = {**_SHARED_CURRENCY, **_OWN_CURRENCY}

#: Month first. Everywhere else not in `_YEAR_FIRST` writes the day first.
_MONTH_FIRST = frozenset(("US", "AS", "GU", "MP", "PR", "UM", "VI", "FM", "MH", "PW", "PH", "BZ"))

#: Year first, as the country's own standard writes a date.
_YEAR_FIRST = frozenset(
    ("CN", "JP", "KR", "KP", "TW", "MN", "HU", "LT", "SE", "CA", "IR", "BT", "MO", "HK")
)

#: CLDR's `firstDay`: where weeks start on Sunday or on Saturday. Everywhere
#: else, Monday. (CLDR's one Friday, the Maldives, is offered Monday: the
#: household setting has no Friday.)
_SUNDAY_FIRST = frozenset(
    (
        "AG", "AS", "BD", "BR", "BS", "BT", "BW", "BZ", "CA", "CN", "CO", "DM", "DO",
        "ET", "GT", "GU", "HK", "HN", "ID", "IL", "IN", "JM", "JP", "KE", "KH", "KR",
        "LA", "MH", "MM", "MO", "MT", "MX", "MZ", "NI", "NP", "PA", "PE", "PH", "PK",
        "PR", "PT", "PY", "SA", "SG", "SV", "TH", "TT", "TW", "UM", "US", "VE", "VI",
        "WS", "YE", "ZA", "ZW",
    )
)
_SATURDAY_FIRST = frozenset(
    ("AE", "AF", "BH", "DJ", "DZ", "EG", "IQ", "IR", "JO", "KW", "LY", "OM", "QA", "SD", "SY")
)


def date_format(code: str) -> str:
    """The household date format a country writes, among the three offered."""
    if code in _MONTH_FIRST:
        return "MM/DD/YYYY"
    if code in _YEAR_FIRST:
        return "YYYY-MM-DD"
    return "DD/MM/YYYY"


def week_start(code: str) -> str:
    """The day a country's weeks start on, as `WeekStart` names it."""
    if code in _SATURDAY_FIRST:
        return "saturday"
    if code in _SUNDAY_FIRST:
        return "sunday"
    return "monday"


def suggestions() -> list[dict[str, str | None]]:
    """Every country with what the region step suggests for it.

    Suggestions only: each one is editable on the step, and nothing here is
    stored -- the household keeps the currency, format and week start chosen,
    not the country. The number format is never suggested: it follows the
    language.
    """
    return [
        {
            "code": code,
            "name": name,
            "currency": CURRENCY.get(code),
            "date_format": date_format(code),
            "week_start": week_start(code),
        }
        for code, name in COUNTRIES
    ]
