import * as React from 'react';
import { Slot } from '@radix-ui/react-slot';
import { cva, type VariantProps } from 'class-variance-authority';
import { cn } from '../lib/utils';

const buttonVariants = cva('reader-button', {
  variants: {
    variant: {
      default: 'reader-button--primary',
      secondary: 'reader-button--secondary',
      outline: 'reader-button--outline',
      ghost: 'reader-button--ghost',
      destructive: 'reader-button--destructive',
    },
    size: {
      default: 'reader-button--default',
      sm: 'reader-button--sm',
      lg: 'reader-button--lg',
      icon: 'reader-button--icon',
    },
  },
  defaultVariants: { variant: 'default', size: 'default' },
});

export interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement>, VariantProps<typeof buttonVariants> {
  asChild?: boolean;
}

export const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, asChild = false, ...props }, ref) => {
    const Component = asChild ? Slot : 'button';
    return <Component ref={ref} className={cn(buttonVariants({ variant, size }), className)} {...props} />;
  },
);
Button.displayName = 'Button';
