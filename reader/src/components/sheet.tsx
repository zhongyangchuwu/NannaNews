import * as React from 'react';
import * as Dialog from '@radix-ui/react-dialog';
import { X } from 'lucide-react';
import { cn } from '../lib/utils';

export const Sheet = Dialog.Root;
export const SheetTrigger = Dialog.Trigger;
export const SheetClose = Dialog.Close;
export const SheetTitle = Dialog.Title;
export const SheetDescription = Dialog.Description;

export const SheetContent = React.forwardRef<
  React.ElementRef<typeof Dialog.Content>,
  React.ComponentPropsWithoutRef<typeof Dialog.Content> & { side?: 'left' | 'right' }
>(({ className, children, side = 'right', ...props }, ref) => (
  <Dialog.Portal>
    <Dialog.Overlay className="reader-sheet-overlay" />
    <Dialog.Content ref={ref} className={cn('reader-sheet-content', `reader-sheet-content--${side}`, className)} {...props}>
      {children}
      <Dialog.Close className="reader-sheet-close" aria-label="关闭筛选面板">
        <X size={18} aria-hidden="true" />
      </Dialog.Close>
    </Dialog.Content>
  </Dialog.Portal>
));
SheetContent.displayName = 'SheetContent';
