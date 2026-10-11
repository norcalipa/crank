// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import * as React from 'react';
import {createPortal} from 'react-dom';
import {lockBackground, unlockBackground} from './modalIsolation';
import CompanyDetails, {
    COMPANY_ID_PLACEHOLDER, CompanyDetailsOrganization, CorrectionSource, companyChatUrl, companySignInUrl,
} from './company/CompanyDetails';
import {openSuggestCompany} from './suggestCompany/controller';
import {openAssistant} from './workspace/store';
import {WORKSPACE_FOCUS_EVENT} from './workspace/types';

// Kept as this module's exports for its existing importers; the definitions
// moved with the content they belong to (issue #486).
export {COMPANY_ID_PLACEHOLDER, companyChatUrl, companySignInUrl};

type Organization = CompanyDetailsOrganization;

interface OrganizationDetailsPopupProps {
    organization: Organization | null;
    visible: boolean;
    onClose: () => void;
    isAuthenticated?: boolean;
    // Server-built login URL carrying a validated `next` back to this page
    // with `?company=<id>` (issue #465 AC-7). Contains
    // COMPANY_ID_PLACEHOLDER where the organization id goes; see
    // crank/views/index.py. Empty when the server emitted none, which
    // simply hides the signed-out CTA rather than inventing a URL here.
    signInUrlTemplate?: string;
}

// The blocking-dialog shell around CompanyDetails: the portal, the overlay,
// the background lock, the focus trap, Escape and the Close button. What the
// dialog shows, and the requests behind it, live in ./company (issue #486).
const OrganizationDetailsPopup: React.FC<OrganizationDetailsPopupProps> = ({
    organization,
    visible,
    onClose,
    isAuthenticated = false,
    signInUrlTemplate = ''
}) => {
    const closeButtonRef = React.useRef<HTMLButtonElement>(null);
    const dialogRef = React.useRef<HTMLDivElement>(null);
    // Element that had focus when the dialog opened (the trigger). Restored on
    // close so keyboard and pointer users return to where they left off.
    const openerRef = React.useRef<HTMLElement | null>(null);

    // Restore focus to the opener on close (WAI-ARIA dialog pattern). If the
    // opener is no longer in the document, defensively blur the active element
    // so focus never lingers on a now-hidden node. Uses the same
    // instanceof HTMLElement guard as the rest of this component.
    const restoreFocusToOpener = () => {
        if (openerRef.current instanceof HTMLElement && openerRef.current.isConnected) {
            openerRef.current.focus();
        } else if (document.activeElement instanceof HTMLElement) {
            document.activeElement.blur();
        }
    };

    // Background isolation (issue #464): while the dialog is open, everything
    // outside it — the app shell, the page content and the modal-external
    // skip link — is inert/aria-hidden, and the actual document scroller
    // (the root element, which carries the viewport overflow under the
    // stylesheet's `html, body { overflow-x: hidden }` rule) is locked along
    // with body. Keyboard focus, assistive-technology virtual navigation,
    // programmatic focus and wheel/touch scrolling therefore cannot reach
    // the page behind the blocking dialog.
    //
    // The opener is captured BEFORE lockBackground() inerts the shell:
    // inerting the shell blurs the trigger (activeElement resets to <body>),
    // so capturing after the lock would record <body> and silently lose the
    // focus-restore target. On close the isolation is released BEFORE focus
    // returns to the opener: the trigger element lives in the (currently
    // inert) background, so restoring earlier would silently fail in real
    // browsers and break the #464 focus-restore contract.
    React.useLayoutEffect(() => {
        if (!visible) {
            return;
        }
        // Capture the opener BEFORE lockBackground(): inerting the background
        // resets document.activeElement to <body> when the (focused) trigger
        // becomes inert, so any capture after this point records <body> and
        // Escape/Close would restore focus nowhere (the deterministic
        // django-e2e focus-restore failure repaired for #465). Runs only on
        // the rising edge of `visible`, so a re-render while the dialog is
        // already open (e.g. the crank:auth-hydrated re-render) can never
        // clobber the original opener with the dialog's own Close button.
        openerRef.current = document.activeElement instanceof HTMLElement
            ? document.activeElement
            : null;
        lockBackground();
        return () => {
            unlockBackground();
            restoreFocusToOpener();
        };
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [visible]);

    // Move focus into the dialog once it opens. A passive effect is safe here:
    // focusing the Close button inside the dialog is unaffected by the
    // background inert applied in the layout effect above.
    React.useEffect(() => {
        if (visible) {
            closeButtonRef.current?.focus();
        }
    }, [visible]);

    React.useEffect(() => {
        // Focusable-element selector for the WAI-ARIA focus trap (issue #464).
        const getFocusableElements = (): HTMLElement[] => {
            const dialog = dialogRef.current;
            if (!dialog) return [];
            return Array.from(dialog.querySelectorAll<HTMLElement>(
                'a[href], button:not([disabled]), input:not([disabled]), '
                + 'select:not([disabled]), textarea:not([disabled]), '
                + '[tabindex]:not([tabindex="-1"])'
            ));
        };

        const handleKeyDown = (event: KeyboardEvent) => {
            if (!visible) return;
            if (event.key === 'Escape') {
                // WAI-ARIA dialog pattern: return focus to the trigger element
                // that opened the dialog before closing (issue #430).
                restoreFocusToOpener();
                onClose();
                return;
            }
            if (event.key === 'Tab') {
                // WAI-ARIA focus trap (issue #464): cycle Tab/Shift+Tab among
                // the dialog's own focusable elements so keyboard focus can
                // never move behind the modal into the page background.
                const focusables = getFocusableElements();
                if (focusables.length === 0) return;
                const first = focusables[0];
                const last = focusables[focusables.length - 1];
                const active = document.activeElement;
                const insideDialog = active instanceof HTMLElement && dialogRef.current?.contains(active);
                if (event.shiftKey) {
                    if (!insideDialog || active === first) {
                        event.preventDefault();
                        last.focus();
                    }
                } else if (!insideDialog || active === last) {
                    event.preventDefault();
                    first.focus();
                }
            }
        };

        document.addEventListener('keydown', handleKeyDown);

        return () => {
            document.removeEventListener('keydown', handleKeyDown);
        };
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [visible, onClose]);

    if (!organization || !visible) {
        return null;
    }

    // Only one blocking dialog is open at a time (issue #464/#471/#477):
    // opening the correction form closes this details dialog.
    const openCorrection = (source: CorrectionSource, fieldKey?: string) => {
        openSuggestCompany({
            kind: 'correction',
            source,
            companyName: organization.name,
            organizationId: organization.id,
            fieldKey,
        });
        onClose();
    };

    const handleCloseClick = (e: React.MouseEvent) => {
        e.stopPropagation();
        // Return focus to the trigger element before close (WAI-ARIA dialog
        // pattern). The close path also releases background isolation and
        // re-restores focus afterwards (see the isolation effect), so the
        // opener receives focus even while it is still inert here.
        restoreFocusToOpener();
        onClose();
    };

    // Issue #479: open the assistant in place with this company as context.
    // openAssistant runs on the next frame so the dialog's unlockBackground()
    // has already released the blocking-surface lock (the #472 yield rule
    // would otherwise close the sheet immediately). CompanyDetails calls this
    // only for a plain click on a page with the workspace host.
    const handleAsk = () => {
        const organizationId = organization.id;
        const organizationName = organization.name;
        onClose();
        window.requestAnimationFrame(() => {
            openAssistant({surface: 'company', organizationId, organizationName});
            // The dialog's opener is gone; ask the shell to move focus into
            // the assistant even when its chat never reaches a composer
            // (initialization error, gated account).
            window.dispatchEvent(new CustomEvent(WORKSPACE_FOCUS_EVENT));
        });
    };

    const handleOverlayClick = (e: React.MouseEvent) => {
        // Only close if clicking directly on the overlay, not its children
        if (e.target === e.currentTarget) {
            // Return focus to the trigger element before close (WAI-ARIA dialog pattern).
            restoreFocusToOpener();
            onClose();
        }
    };

    // Render the dialog into a portal on <body>: the blocking dialog must not
    // live inside the (inert) background containers while it is open.
    // Fixed-position overlays are page-level by convention.
    return createPortal(
        <div className="popup-overlay" data-testid="popup-overlay" onClick={handleOverlayClick}>
            <div ref={dialogRef} className="popup-details card bg-dark" role="dialog" aria-modal="true"
                 aria-labelledby="organization-details-title">
                <div className="card-header bg-dark d-flex justify-content-between align-items-center">
                    <h2 id="organization-details-title">{organization.name}</h2>
                    <button
                        ref={closeButtonRef}
                        type="button"
                        className="btn-close btn-close-white"
                        aria-label="Close"
                        onClick={handleCloseClick}
                    ></button>
                </div>
                <CompanyDetails
                    organization={organization}
                    isAuthenticated={isAuthenticated}
                    signInUrlTemplate={signInUrlTemplate}
                    onAsk={handleAsk}
                    onSuggestCorrection={openCorrection}
                />
            </div>
        </div>,
        document.body
    );
};

export default OrganizationDetailsPopup;
