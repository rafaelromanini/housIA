import type { Metadata, Viewport } from "next";
import { Atkinson_Hyperlegible } from "next/font/google";

import { DemoBanner, Disclaimer } from "@/components/demo-banner";
import { Providers } from "@/components/providers";
import { ServiceWorker } from "@/components/service-worker";

import "./globals.css";

const atkinson = Atkinson_Hyperlegible({
  subsets: ["latin"],
  weight: ["400", "700"],
  variable: "--font-atkinson",
  display: "swap",
});

export const metadata: Metadata = {
  title: { default: "HousIA", template: "%s · HousIA" },
  description: "Suporte à decisão em transições de cuidado — ambiente de demonstração com dados sintéticos.",
  manifest: "/manifest.webmanifest",
  icons: { icon: "/icon-192.png", apple: "/apple-touch-icon.png" },
  appleWebApp: { capable: true, title: "HousIA", statusBarStyle: "default" },
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  themeColor: "#0e7490",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="pt-BR" className={atkinson.variable}>
      <body className="min-h-dvh flex flex-col antialiased">
        <DemoBanner />
        <div className="flex-1 flex flex-col">
          <Providers>{children}</Providers>
          <ServiceWorker />
        </div>
        <Disclaimer />
      </body>
    </html>
  );
}
