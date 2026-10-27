// Embeddable entry point for the LoanDesk trader UI: <DeskChat /> plus the client API and store.
export { DeskChat, DISCLAIMER } from "./DeskChat";
export type { TicketEvent } from "./components/ChipCard";
export { api, IB_URL } from "./lib/api";
export type { Chip, Instrument, Message, Room, User } from "./lib/api";
export { useStore } from "./lib/store";
