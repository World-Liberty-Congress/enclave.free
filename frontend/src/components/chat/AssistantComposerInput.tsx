import { ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { ComposerPrimitive } from '@assistant-ui/react';
import { Send, Square } from 'lucide-react';
import { IconButton } from '../ui';

interface AssistantComposerInputProps {
  disabled?: boolean;
  onStop?: () => void;
  placeholder?: string;
  toolbar?: ReactNode;
}

export function AssistantComposerInput({
  disabled,
  onStop,
  placeholder,
  toolbar,
}: AssistantComposerInputProps) {
  const { t } = useTranslation();
  const defaultPlaceholder = t('chat.input.placeholder');

  return (
    <div className="border-t border-border bg-surface px-3 py-3 shadow-[0_-1px_3px_rgba(0,0,0,0.03)] sm:px-4">
      <div className="mx-auto max-w-3xl">
        <div className="input-container overflow-hidden rounded-2xl bg-surface-raised!">
          {toolbar && (
            <div
              role="group"
              aria-label={t('chat.composerContextAria', 'Composer context')}
              className="flex flex-wrap items-center gap-2 border-b border-border/50 bg-surface-overlay/30 px-3 py-2.5"
            >
              {toolbar}
            </div>
          )}
          <ComposerPrimitive.Root className="flex items-end gap-2 p-2">
            <ComposerPrimitive.Input
              aria-label={placeholder || defaultPlaceholder}
              placeholder={placeholder || defaultPlaceholder}
              disabled={disabled}
              rows={1}
              submitMode="enter"
              className="max-h-40 flex-1 resize-none border-none bg-transparent px-2 py-2 text-[15px] leading-relaxed text-text placeholder:text-text-muted focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
            />
            {onStop ? (
              <IconButton
                onClick={onStop}
                label={t('chat.actions.stop')}
                variant="secondary"
                className="rounded-xl"
              >
                <Square className="h-5 w-5" aria-hidden="true" />
              </IconButton>
            ) : (
              <ComposerPrimitive.Send
                aria-label={t('chat.input.sendLabel')}
                title={t('chat.input.sendTitle')}
                className="inline-flex h-10 w-10 shrink-0 items-center justify-center rounded-xl border border-primary bg-primary text-primary-foreground shadow-sm transition-all hover:bg-primary-hover hover:shadow-md focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50 disabled:shadow-none"
              >
                <Send className="h-5 w-5" aria-hidden="true" />
              </ComposerPrimitive.Send>
            )}
          </ComposerPrimitive.Root>
        </div>
      </div>
    </div>
  );
}
