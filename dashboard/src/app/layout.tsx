import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Universal AI Gateway — Operational Control Plane",
  description:
    "Self-hosted multi-provider AI routing, credential rotation, MCP tool orchestration, and usage analytics.",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" className="dark" suppressHydrationWarning>
      <body className="min-h-screen bg-[#090D16] text-gray-100 antialiased" suppressHydrationWarning>
        {children}
      </body>
    </html>
  );
}

