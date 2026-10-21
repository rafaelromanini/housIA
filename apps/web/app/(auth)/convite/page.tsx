import { Suspense } from "react";

import { InviteForm } from "@/components/auth/invite-form";

export const metadata = { title: "Convite" };

export default function Page() {
  return (
    <Suspense>
      <InviteForm />
    </Suspense>
  );
}
