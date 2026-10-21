import { Suspense } from "react";

import { VerifyView } from "@/components/auth/verify-view";

export const metadata = { title: "Confirmar e-mail" };

export default function Page() {
  return (
    <Suspense>
      <VerifyView />
    </Suspense>
  );
}
