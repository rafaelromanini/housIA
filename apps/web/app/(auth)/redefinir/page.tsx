import { Suspense } from "react";

import { ResetForm } from "@/components/auth/reset-form";

export const metadata = { title: "Nova senha" };

export default function Page() {
  return (
    <Suspense>
      <ResetForm />
    </Suspense>
  );
}
