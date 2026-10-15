import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";

import { cn } from "@/lib/utils";

const alertVariants = cva("relative w-full rounded-lg border p-3 text-sm flex gap-3 [&>svg]:size-5 [&>svg]:shrink-0 [&>svg]:mt-0.5", {
  variants: {
    variant: {
      default: "bg-card border-border",
      critico: "bg-critico-bg border-critico-border text-critico",
      atencao: "bg-atencao-bg border-atencao-border text-atencao",
      info: "bg-info-bg border-info-border text-info",
      ok: "bg-ok-bg border-ok-border text-ok",
    },
  },
  defaultVariants: { variant: "default" },
});

export function Alert({ className, variant, ...props }: React.HTMLAttributes<HTMLDivElement> & VariantProps<typeof alertVariants>) {
  return <div role="alert" className={cn(alertVariants({ variant }), className)} {...props} />;
}
