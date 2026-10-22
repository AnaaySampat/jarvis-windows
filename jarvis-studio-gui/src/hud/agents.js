// The agent row (backend agents.py). Auto lets the coordinator choose per request; a
// picked agent gets every typed or spoken request until another is picked, and
// "@handle" in a message reaches one from anywhere. Each agent has its own colour.
export const AGENT_PICKS = [
  { id: "auto", name: "Auto", handle: "auto", color: "var(--accent)",
    hint: "JARVIS picks the agents for each request and hands work between them" },
  { id: "computer", name: "Desktop", handle: "desktop", color: "#f5a524",
    hint: "Windows apps — opens, types, saves, clicks" },
  { id: "browser", name: "Browser", handle: "browser", color: "#5b8cff",
    hint: "Websites, in your own signed-in Chrome" },
  { id: "research", name: "Research", handle: "research", color: "#a07cff",
    hint: "Reads several sites in the background and reports with sources" },
  { id: "files", name: "Files", handle: "files", color: "#3ecf8e",
    hint: "Lists, reads and summarises your files" },
  { id: "scheduling", name: "Scheduler", handle: "scheduler", color: "#ff6fa8",
    hint: "Reminders, alarms, the agenda, routines" },
  { id: "content", name: "Content", handle: "content", color: "#ff8f4c",
    hint: "Writes posts, drafts and documents; makes images" },
];

export const agentPick = (id) => AGENT_PICKS.find((a) => a.id === id) || AGENT_PICKS[0];
