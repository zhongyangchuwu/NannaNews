import * as React from 'react';
import { cva, type VariantProps } from 'class-variance-authority';
import { cn } from '../lib/utils';

const badgeVariants = cva('reader-badge', {
  variants: {
    variant: {
      default: 'reader-badge--default',
      muted: 'reader-badge--muted',
      outline: 'reader-badge--outline',
      accent: 'reader-badge--accent',
      warning: 'reader-badge--warning',
    },
  },
  defaultVariants: { variant: 'default' },
});

export interface BadgeProps extends React.HTMLAttributes<HTMLSpanElement>, VariantProps<typeof badgeVariants> {}

export function Badge({ className, variant, ...props }: BadgeProps) {
  return <span className={cn(badgeVariants({ variant }), className)} {...props} />;
}
