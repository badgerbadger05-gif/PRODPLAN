import type { ButtonHTMLAttributes } from 'react'

export type ButtonVariant = 'default' | 'primary' | 'success'

export type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: ButtonVariant
}

export function Button({
  variant = 'default',
  className,
  ...props
}: ButtonProps) {
  const variantClass = variant === 'default' ? '' : variant
  const classes = [variantClass, className].filter(Boolean).join(' ') || undefined
  return <button {...props} className={classes} />
}
