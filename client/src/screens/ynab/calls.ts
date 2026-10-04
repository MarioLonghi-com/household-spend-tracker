/**
 * The four calls the YNAB wizard makes (#183).
 *
 * The routes keep nothing between calls, so every one of them after `/plans`
 * carries the source again: the file as multipart, or the token and the plan
 * id as form fields. The token is only ever a form field on a POST body -- it
 * is never put in a URL, where a proxy log or the browser history would keep it.
 */

import { api } from "../../lib/api";
import type { Analysis, ImportPlan, ImportReport, Source, YnabPlan } from "./types";

const base = (householdId: string) => `/households/${householdId}/one-time-import/ynab`;

function sourceForm(source: Source): FormData {
  const form = new FormData();
  form.append("via", source.via);
  if (source.via === "csv") {
    form.append("file", source.file);
  } else {
    form.append("token", source.token);
    form.append("plan_id", source.planId);
  }
  return form;
}

export function listPlans(householdId: string, token: string) {
  return api.post<{ plans: YnabPlan[] }>(`${base(householdId)}/plans`, { token });
}

export function analyse(
  householdId: string,
  source: Source,
  choices: { currency?: string | null; dateFormat?: string | null } = {},
) {
  const form = sourceForm(source);
  if (choices.currency) form.append("currency", choices.currency);
  if (choices.dateFormat) form.append("date_format", choices.dateFormat);
  return api.upload<Analysis>(`${base(householdId)}/analyse`, form);
}

function planned(householdId: string, step: "preview" | "commit", source: Source, plan: ImportPlan) {
  const form = sourceForm(source);
  form.append("plan", JSON.stringify(plan));
  return api.upload<ImportReport>(`${base(householdId)}/${step}`, form);
}

export const preview = (householdId: string, source: Source, plan: ImportPlan) =>
  planned(householdId, "preview", source, plan);

export const commit = (householdId: string, source: Source, plan: ImportPlan) =>
  planned(householdId, "commit", source, plan);
