/**
 * The words a new household is seeded with (#268).
 *
 * The server writes them -- `app/seed_words.py` holds the same ids and the
 * same English, and `tests/test_seed_words.py` fails when the two disagree.
 * They are here so that extraction puts them in the catalogs beside every
 * other message, where a translator meets them; the server reads only the
 * reviewed ones, through `app/seed_catalog.json` (`scripts/seed_catalog.py`).
 *
 * Once seeded they are a household's own names, renamed like anything typed.
 */

import { msg } from "@lingui/core/macro";
import { i18n, SERVED_LOCALES, SOURCE_LOCALE } from "./i18n";

export const SEED_WORDS = [
  msg({
    id: "seed.opening_balance",
    message: "Opening balance",
    comment:
      "Payee and memo of the row holding an account's starting balance. See GLOSSARY.md",
  }),
  msg({
    id: "seed.group.income",
    message: "Income",
    comment:
      "Name of a category group seeded into a new household; the person can rename it",
  }),
  msg({
    id: "seed.category.salary",
    message: "Salary",
    comment:
      "Name of a category seeded into a new household; the person can rename it",
  }),
  msg({
    id: "seed.category.other_income",
    message: "Other Income",
    comment:
      "Name of a category seeded into a new household; the person can rename it",
  }),
  msg({
    id: "seed.group.bills",
    message: "Bills",
    comment:
      "Name of a category group seeded into a new household; the person can rename it",
  }),
  msg({
    id: "seed.category.rent_mortgage",
    message: "Rent / Mortgage",
    comment:
      "Name of a category seeded into a new household; the person can rename it",
  }),
  msg({
    id: "seed.category.electricity",
    message: "Electricity",
    comment:
      "Name of a category seeded into a new household; the person can rename it",
  }),
  msg({
    id: "seed.category.water",
    message: "Water",
    comment:
      "Name of a category seeded into a new household; the person can rename it",
  }),
  msg({
    id: "seed.category.internet",
    message: "Internet",
    comment:
      "Name of a category seeded into a new household; the person can rename it",
  }),
  msg({
    id: "seed.category.phone",
    message: "Phone",
    comment:
      "Name of a category seeded into a new household; the person can rename it",
  }),
  msg({
    id: "seed.category.insurance",
    message: "Insurance",
    comment:
      "Name of a category seeded into a new household; the person can rename it",
  }),
  msg({
    id: "seed.group.everyday",
    message: "Everyday",
    comment:
      "Name of a seeded category group: the spending of daily life. The person can rename it",
  }),
  msg({
    id: "seed.category.groceries",
    message: "Groceries",
    comment:
      "Name of a category seeded into a new household; the person can rename it",
  }),
  msg({
    id: "seed.category.eating_out",
    message: "Eating Out",
    comment:
      "Name of a seeded category: restaurants, cafés, takeaway. The person can rename it",
  }),
  msg({
    id: "seed.category.transport",
    message: "Transport",
    comment:
      "Name of a seeded category: fares, fuel, taxis. The person can rename it",
  }),
  msg({
    id: "seed.category.household",
    message: "Household",
    comment:
      "Name of a seeded category: things for the home. Not the household of the glossary",
  }),
  msg({
    id: "seed.category.health",
    message: "Health",
    comment:
      "Name of a category seeded into a new household; the person can rename it",
  }),
  msg({
    id: "seed.group.quality_of_life",
    message: "Quality of Life",
    comment:
      "Name of a seeded category group: spending by choice, not need. The person can rename it",
  }),
  msg({
    id: "seed.category.travel",
    message: "Travel",
    comment:
      "Name of a category seeded into a new household; the person can rename it",
  }),
  msg({
    id: "seed.category.subscriptions",
    message: "Subscriptions",
    comment:
      "Name of a category seeded into a new household; the person can rename it",
  }),
  msg({
    id: "seed.category.gifts",
    message: "Gifts",
    comment:
      "Name of a category seeded into a new household; the person can rename it",
  }),
  msg({
    id: "seed.category.hobbies",
    message: "Hobbies",
    comment:
      "Name of a category seeded into a new household; the person can rename it",
  }),
  msg({
    id: "seed.group.non_monthly",
    message: "Non-Monthly",
    comment:
      "Name of a seeded category group: bills that come less often than monthly",
  }),
  msg({
    id: "seed.category.car_maintenance",
    message: "Car Maintenance",
    comment:
      "Name of a category seeded into a new household; the person can rename it",
  }),
  msg({
    id: "seed.category.home_maintenance",
    message: "Home Maintenance",
    comment:
      "Name of a category seeded into a new household; the person can rename it",
  }),
  msg({
    id: "seed.category.annual_fees",
    message: "Annual Fees",
    comment:
      "Name of a seeded category: yearly charges such as a card fee. The person can rename it",
  }),
];

/**
 * What a request that seeds a household sends about its language: the
 * device's locale when it is served and not English, and otherwise nothing,
 * so the body is exactly what it was before (#268).
 */
export function seedLocale(
  locale: string = i18n.locale,
  served: readonly string[] = SERVED_LOCALES,
): { locale?: string } {
  return locale !== SOURCE_LOCALE && served.includes(locale) ? { locale } : {};
}
