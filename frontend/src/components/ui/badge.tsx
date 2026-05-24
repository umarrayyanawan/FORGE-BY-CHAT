import { cn } from "@/lib/utils";
import type { HTMLAttributes } from "react";

interface BadgeProps extends HTMLAttributes<HTMLSpanElement> {
  variant?: "default" | "success" | "warning" | "error" | "info";
}

const variants: Record<string, string> = {
  default: "bg-slate-800 text-slate-300",
  success: "bg-green-900/50 text-green-400 border border-green-800",
  warning: "bg-yellow-900/50 text-yellow-400 border border-yellow-800",
  error: "bg-red-900/50 text-red-400 border border-red-800",
  info: "bg-indigo-900/50 text-indigo-400 border border-indigo-800",
};

export function Badge({ className, variant = "default", ...props }: BadgeProps) {
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium",
        variants[variant],
        className
      )}
      {...props}
    />
  );
}
