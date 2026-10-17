// Small stroke icons for the control bar (drawn for this project).
import type { ReactNode } from 'react'

const I = ({ children }: { children: ReactNode }) => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round" aria-hidden>
    {children}
  </svg>
)

export const MicIcon = () => (
  <I>
    <rect x="9" y="3" width="6" height="11" rx="3" />
    <path d="M5 11a7 7 0 0 0 14 0M12 18v3" />
  </I>
)
export const MicOffIcon = () => (
  <I>
    <path d="M15 9.3V6a3 3 0 0 0-5.7-1.3M9 9v2a3 3 0 0 0 4.9 2.3M5 11a7 7 0 0 0 11.5 5.4M19 11a7 7 0 0 1-.6 2.8M12 18v3M3 3l18 18" />
  </I>
)
export const CamIcon = () => (
  <I>
    <rect x="3" y="6" width="13" height="12" rx="3" />
    <path d="M16 10.5 21 8v8l-5-2.5" />
  </I>
)
export const CamOffIcon = () => (
  <I>
    <path d="M16 16v1a1 1 0 0 1-1 1H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h1M10 6h5a1 1 0 0 1 1 1v3.5L21 8v8M3 3l18 18" />
  </I>
)
export const BoardIcon = () => (
  <I>
    <rect x="3" y="4" width="18" height="13" rx="2" />
    <path d="M8 21h8M12 17v4M7 13l3-3 2 2 4-4" />
  </I>
)
export const CaptionsIcon = () => (
  <I>
    <rect x="3" y="5" width="18" height="14" rx="3" />
    <path d="M10 10.5a2 2 0 1 0 0 3M16.5 10.5a2 2 0 1 0 0 3" />
  </I>
)
export const HandIcon = () => (
  <I>
    <path d="M8 13V5.5a1.5 1.5 0 0 1 3 0V11M11 10V4.5a1.5 1.5 0 0 1 3 0V11M14 10.5V6a1.5 1.5 0 0 1 3 0v8a6 6 0 0 1-6 6h-.5a6 6 0 0 1-4.8-2.4L3.5 14.6a1.5 1.5 0 0 1 2.3-1.9L8 15" />
  </I>
)
export const PeopleIcon = () => (
  <I>
    <circle cx="9" cy="8" r="3.5" />
    <path d="M2.5 20a6.5 6.5 0 0 1 13 0M16 4.5a3.5 3.5 0 0 1 0 7M18 14a6.5 6.5 0 0 1 3.5 6" />
  </I>
)
export const SparkIcon = () => (
  <I>
    <path d="M12 3v4M12 17v4M3 12h4M17 12h4M6 6l2.5 2.5M15.5 15.5 18 18M18 6l-2.5 2.5M8.5 15.5 6 18" />
  </I>
)
export const LeaveIcon = () => (
  <I>
    <path d="M3 14.5c4.8-4.6 13.2-4.6 18 0l-2.2 2.2-3.3-1.6v-2.6a12 12 0 0 0-7 0v2.6l-3.3 1.6z" />
  </I>
)
export const TalkIcon = () => (
  <I>
    <path d="M4 5h16v10H9l-5 4z" />
    <path d="M9 10h.01M12 10h.01M15 10h.01" />
  </I>
)
export const SpeakerOffIcon = () => (
  <I>
    <path d="M11 5 6 9H3v6h3l5 4zM16 9l5 5M21 9l-5 5" />
  </I>
)
export const SpeakerIcon = () => (
  <I>
    <path d="M11 5 6 9H3v6h3l5 4zM15.5 8.5a5 5 0 0 1 0 7M18.5 5.5a9 9 0 0 1 0 13" />
  </I>
)
export const CloseIcon = () => (
  <I>
    <path d="M6 6l12 12M18 6 6 18" />
  </I>
)
export const ShareIcon = () => (
  <I>
    <rect x="3" y="4" width="18" height="13" rx="2" />
    <path d="M12 14V8M9 10.5 12 7.5l3 3M8 21h8" />
  </I>
)
