import { ReactNode } from 'react';
import { isPxDemo } from '../../branding/pxDemo';
import { PxBrand } from '../../branding/PxBrand';
import { PxWelcome } from '../../branding/PxWelcome';
import { InstanceLogo } from '../shared/InstanceLogo';

interface OnboardingCardProps {
  children: ReactNode;
  footer?: ReactNode;
  title?: string;
  subtitle?: string;
  size?: 'md' | 'lg' | 'xl';
  topRight?: ReactNode;
  presentation?: 'standard' | 'entry';
}

export function OnboardingCard({
  children,
  footer,
  title,
  subtitle,
  size,
  topRight,
  presentation = 'standard',
}: OnboardingCardProps) {
  const isEntry = isPxDemo && presentation === 'entry';
  const maxWidthClass = {
    md: 'max-w-2xl',
    lg: 'max-w-4xl',
    xl: 'max-w-5xl',
  }[size ?? 'md'];

  const content = (
    <div
      className={`${isEntry ? 'px-onboarding ' : ''}relative min-h-screen w-full overflow-x-hidden bg-gradient-to-br from-surface via-surface to-surface-raised/30 flex flex-col items-center justify-center p-4`}
    >
      {topRight && (
        <div className="px-onboarding-actions absolute top-4 inset-x-4 flex min-w-0 justify-end">
          {topRight}
        </div>
      )}

      <div className={`w-full min-w-0 ${maxWidthClass}`}>
        {!isEntry &&
          (isPxDemo ? (
            <div className="mb-6">
              <PxBrand />
            </div>
          ) : (
            <InstanceLogo />
          ))}

        <div className="card w-full max-w-full rounded-3xl p-6 sm:p-10 min-w-0 overflow-hidden animate-fade-in-up">
          {(title || subtitle) && (
            <div className="text-center mb-8 min-w-0">
              {title && (
                <h1 className="heading-xl text-balance break-words">{title}</h1>
              )}
              {subtitle && (
                <p className="text-sm text-text-muted mt-2 max-w-md mx-auto text-balance break-words">
                  {subtitle}
                </p>
              )}
            </div>
          )}
          {children}
          {isEntry && footer && <div className="px-entry-footer">{footer}</div>}
        </div>

        {!isEntry && footer && (
          <div className="text-center mt-6 text-sm text-text-muted animate-fade-in">
            {footer}
          </div>
        )}
      </div>
    </div>
  );

  return isEntry ? <PxWelcome>{content}</PxWelcome> : content;
}
