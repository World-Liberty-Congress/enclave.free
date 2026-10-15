import {
  forwardRef,
  useId,
  type InputHTMLAttributes,
  type SelectHTMLAttributes,
  type TextareaHTMLAttributes,
} from 'react';
import { cx } from './utils';

interface FieldChromeProps {
  id?: string;
  label: string;
  description?: string;
  error?: string;
  className?: string;
  hideLabel?: boolean;
}

export interface TextFieldProps
  extends
    FieldChromeProps,
    Omit<InputHTMLAttributes<HTMLInputElement>, 'className' | 'id'> {}

export interface TextareaProps
  extends
    FieldChromeProps,
    Omit<TextareaHTMLAttributes<HTMLTextAreaElement>, 'className' | 'id'> {}

export interface SelectFieldProps
  extends
    FieldChromeProps,
    Omit<SelectHTMLAttributes<HTMLSelectElement>, 'className' | 'id'> {}

function FieldText({
  descriptionId,
  description,
  errorId,
  error,
}: {
  descriptionId: string;
  description?: string;
  errorId: string;
  error?: string;
}) {
  if (error) {
    return (
      <p id={errorId} className="text-xs text-error">
        {error}
      </p>
    );
  }

  if (description) {
    return (
      <p id={descriptionId} className="text-xs text-text-muted">
        {description}
      </p>
    );
  }

  return null;
}

export const TextField = forwardRef<HTMLInputElement, TextFieldProps>(
  function TextField(
    {
      id,
      label,
      description,
      error,
      className,
      hideLabel,
      type = 'text',
      ...props
    },
    ref
  ) {
    const generatedId = useId();
    const inputId = id ?? generatedId;
    const descriptionId = `${inputId}-description`;
    const errorId = `${inputId}-error`;
    const describedBy = error
      ? errorId
      : description
        ? descriptionId
        : undefined;
    const {
      'aria-describedby': ariaDescribedBy,
      'aria-invalid': ariaInvalid,
      ...inputProps
    } = props;
    const finalAriaDescribedBy = [ariaDescribedBy, describedBy]
      .filter(Boolean)
      .join(' ');
    const finalAriaInvalid = error ? true : (ariaInvalid ?? undefined);

    return (
      <div className={cx('flex flex-col gap-2', className)}>
        <label
          className={cx(
            'text-sm font-medium text-text',
            hideLabel && 'sr-only'
          )}
          htmlFor={inputId}
        >
          {label}
        </label>
        <span
          className={cx(
            'input-container flex min-h-10 items-center px-3',
            error && 'has-error'
          )}
        >
          <input
            ref={ref}
            id={inputId}
            type={type}
            className="input-field text-base sm:text-sm"
            {...inputProps}
            aria-describedby={finalAriaDescribedBy || undefined}
            aria-invalid={finalAriaInvalid}
          />
        </span>
        <FieldText
          descriptionId={descriptionId}
          description={description}
          errorId={errorId}
          error={error}
        />
      </div>
    );
  }
);

export const Textarea = forwardRef<HTMLTextAreaElement, TextareaProps>(
  function Textarea(
    { id, label, description, error, className, hideLabel, rows = 4, ...props },
    ref
  ) {
    const generatedId = useId();
    const textareaId = id ?? generatedId;
    const descriptionId = `${textareaId}-description`;
    const errorId = `${textareaId}-error`;
    const describedBy = error
      ? errorId
      : description
        ? descriptionId
        : undefined;
    const {
      'aria-describedby': ariaDescribedBy,
      'aria-invalid': ariaInvalid,
      ...textareaProps
    } = props;
    const finalAriaDescribedBy = [ariaDescribedBy, describedBy]
      .filter(Boolean)
      .join(' ');
    const finalAriaInvalid = error ? true : (ariaInvalid ?? undefined);

    return (
      <div className={cx('flex flex-col gap-2', className)}>
        <label
          className={cx(
            'text-sm font-medium text-text',
            hideLabel && 'sr-only'
          )}
          htmlFor={textareaId}
        >
          {label}
        </label>
        <span
          className={cx(
            'input-container flex px-3 py-2.5',
            error && 'has-error'
          )}
        >
          <textarea
            ref={ref}
            id={textareaId}
            rows={rows}
            className="input-field min-h-24 resize-y text-base sm:text-sm"
            {...textareaProps}
            aria-describedby={finalAriaDescribedBy || undefined}
            aria-invalid={finalAriaInvalid}
          />
        </span>
        <FieldText
          descriptionId={descriptionId}
          description={description}
          errorId={errorId}
          error={error}
        />
      </div>
    );
  }
);

export const SelectField = forwardRef<HTMLSelectElement, SelectFieldProps>(
  function SelectField(
    { id, label, description, error, className, hideLabel, children, ...props },
    ref
  ) {
    const generatedId = useId();
    const selectId = id ?? generatedId;
    const descriptionId = `${selectId}-description`;
    const errorId = `${selectId}-error`;
    const describedBy = error
      ? errorId
      : description
        ? descriptionId
        : undefined;
    const {
      'aria-describedby': ariaDescribedBy,
      'aria-invalid': ariaInvalid,
      ...selectProps
    } = props;
    const finalAriaDescribedBy = [ariaDescribedBy, describedBy]
      .filter(Boolean)
      .join(' ');
    const finalAriaInvalid = error ? true : (ariaInvalid ?? undefined);

    return (
      <div className={cx('flex flex-col gap-2', className)}>
        <label
          className={cx(
            'text-sm font-medium text-text',
            hideLabel && 'sr-only'
          )}
          htmlFor={selectId}
        >
          {label}
        </label>
        <span
          className={cx(
            'input-container flex min-h-10 items-center px-3',
            error && 'has-error'
          )}
        >
          <select
            ref={ref}
            id={selectId}
            className="input-field text-base sm:text-sm"
            {...selectProps}
            aria-describedby={finalAriaDescribedBy || undefined}
            aria-invalid={finalAriaInvalid}
          >
            {children}
          </select>
        </span>
        <FieldText
          descriptionId={descriptionId}
          description={description}
          errorId={errorId}
          error={error}
        />
      </div>
    );
  }
);
