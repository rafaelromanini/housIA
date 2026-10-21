import Link from "next/link";
import { Stethoscope } from "lucide-react";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

export function AuthCard({
  titulo,
  descricao,
  children,
  rodape,
}: {
  titulo: string;
  descricao?: string;
  children: React.ReactNode;
  rodape?: React.ReactNode;
}) {
  return (
    <main className="flex flex-1 items-center justify-center p-4">
      <Card className="w-full max-w-md">
        <CardHeader>
          <Link href="/" className="flex items-center gap-2 text-primary min-h-11 w-fit">
            <Stethoscope aria-hidden className="size-7" />
            <span className="text-2xl font-bold">HousIA</span>
          </Link>
          <CardTitle className="text-lg">{titulo}</CardTitle>
          {descricao && <CardDescription>{descricao}</CardDescription>}
        </CardHeader>
        <CardContent className="space-y-4">
          {children}
          {rodape && <div className="border-t pt-4 text-sm space-y-1">{rodape}</div>}
        </CardContent>
      </Card>
    </main>
  );
}

export const linkClass = "font-bold text-primary underline underline-offset-2 hover:no-underline";
