import type { HTMLAttributes } from 'react'

export type StatusBadgeProps = HTMLAttributes<HTMLSpanElement> & {
  size?: 'default' | 'small'
  tone?: string
}

export function StatusBadge({
  size = 'default',
  tone,
  className,
  ...props
}: StatusBadgeProps) {
  const classes = [size === 'small' ? 'miniPill' : 'pill', tone, className]
    .filter(Boolean)
    .join(' ')
  return <span {...props} className={classes} />
}
