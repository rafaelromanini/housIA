"use client";

import { createContext, useContext } from "react";

import type { PatientDetail } from "@/lib/api";

export interface PatientCtx {
  patient: PatientDetail;
  reload: () => Promise<void>;
}

export const PatientContext = createContext<PatientCtx | null>(null);

export function usePatient(): PatientCtx {
  const ctx = useContext(PatientContext);
  if (!ctx) throw new Error("usePatient precisa de <PatientShell>");
  return ctx;
}
