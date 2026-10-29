import { useEffect } from "react";
import { Rail, Toasts, TopBar } from "./components/Chrome";
import { CommandPalette } from "./components/CommandPalette";
import { useKeys } from "./lib/hooks";
import { useDesk, VIEWS } from "./lib/store";
import { useStream } from "./lib/stream";
import { Agent } from "./views/Agent";
import { Audit } from "./views/Audit";
import { BlotterView } from "./views/Blotter";
import { Chat } from "./views/Chat";
import { Locates } from "./views/Locates";
import { Market } from "./views/Market";
import { Paste } from "./views/Paste";
import { Ticket } from "./views/Ticket";

const SCREENS = { blotter: BlotterView, locates: Locates, ticket: Ticket, market: Market, agent: Agent, paste: Paste,
  audit: Audit, chat: Chat };

export function App() {
  useStream();
  const view = useDesk((s) => s.view);
  const setView = useDesk((s) => s.setView);
  const theme = useDesk((s) => s.theme);
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
  }, [theme]);
  useKeys((e) => {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    const v = VIEWS.find((x) => x.key === e.key);
    if (v) {
      setView(v.id);
      e.preventDefault();
    }
  }, []);
  const Screen = SCREENS[view];
  return (
    <div className="h-full flex flex-col">
      <TopBar />
      <div className="flex-1 flex min-h-0">
        <Rail />
        <main className="flex-1 min-w-0 min-h-0 p-3">
          <Screen />
        </main>
      </div>
      <CommandPalette />
      <Toasts />
    </div>
  );
}
