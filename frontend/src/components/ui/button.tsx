import { cn } from "@/lib/utils";
import type { ButtonHTMLAttributes } from "react";

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: "primary" | "ghost" | "destructive";
  size?: "sm" | "md" | "lg";
}

const variants: Record<string, string> = {
  primary: "bg-forge-accent hover:bg-forge-accent-hover text-white",
  ghost: "bg-transparent hover:bg-forge-border text-forge-muted hover:text-forge-text",
  destructive: "bg-red-600 hover:bg-red-700 text-white",
};

const sizes: Record<string, string> = {
  sm: "px-3 py-1.5 text-xs",
  md: "px-4 py-2 text-sm",
  lg: "px-6 py-3 text-base",
};

export function Button({
  className,
  variant = "primary",
  size = "md",
  disabled,
  ...props
}: ButtonProps) {
  return (
    <button
      className={cn(
        "inline-flex items-center justify-center rounded-lg font-medium transition-colors focus:outline-none focus:ring-2 focus:ring-forge-accent focus:ring-offset-2 focus:ring-offset-forge-bg disabled:cursor-not-allowed disabled:opacity-50",
        variants[variant],
        sizes[size],
        className
      )}
      disabled={disabled}
      {...props}
    />
  );
}
