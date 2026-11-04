import { useEffect } from "react";
import { BulkBorrow } from "./components/BulkBorrow";
import { Shell, Toasts } from "./components/Chrome";
import { CommandPalette } from "./components/CommandPalette";
import { useKeys } from "./lib/hooks";
import { useDesk, VIEWS, WORKSPACES } from "./lib/store";
import { useStream } from "./lib/stream";
import { Agent } from "./views/Agent";
import { Audit } from "./views/Audit";
import { BlotterView } from "./views/Blotter";
import { Chat } from "./views/Chat";
import { Locates } from "./views/Locates";
import { Market } from "./views/Market";
import { Paste } from "./views/Paste";
import { ShortListView } from "./views/ShortList";
import { Ticket } from "./views/Ticket";

const SCREENS = { blotter: BlotterView, locates: Locates, ticket: Ticket, market: Market, agent: Agent, paste: Paste,
  audit: Audit, chat: Chat, shorts: ShortListView };

export function App() {
  useStream();
  const view = useDesk((s) => s.view);
  const openWorkspace = useDesk((s) => s.openWorkspace);
  const theme = useDesk((s) => s.theme);
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
  }, [theme]);
  useKeys((e) => {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    const w = WORKSPACES.find((x) => x.key === e.key);
    if (w) {
      openWorkspace(w.id);
      e.preventDefault();
    }
  }, []);
  const Screen = SCREENS[view];
  return (
    <>
      <Shell>
        <h1 className="title m-0 shrink-0">{VIEWS.find((v) => v.id === view)?.label}</h1>
        <div className="flex-1 min-h-0 flex flex-col">
          <Screen />
        </div>
      </Shell>
      <BulkBorrow />
      <CommandPalette />
      <Toasts />
    </>
  );
}
