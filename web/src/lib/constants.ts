import type { AgentLayer } from "@/types/agent-data";

export const VERSION_ORDER = [
  "s01",
  "s02",
  "s03",
  "s04",
  "s05",
  "s06",
  "s07",
  "s08",
  "s09",
  "s10",
  "s11",
  "s12",
  "s13",
  "s14",
  "s15",
  "s16",
  "s17",
  "s18",
  "s19",
] as const;

export const LEARNING_PATH = VERSION_ORDER;

export type VersionId = typeof LEARNING_PATH[number];

export const VERSION_META: Record<string, {
  title: string;
  subtitle: string;
  coreAddition: string;
  keyInsight: string;
  layer: AgentLayer;
  prevVersion: string | null;
}> = {
  s01: {
    title: "The Agent Loop",
    subtitle: "One Loop Is All You Need",
    coreAddition: "Minimal model/tool loop",
    keyInsight: "The smallest useful agent is a loop that calls the model, runs tools, and feeds results back.",
    layer: "tools",
    prevVersion: null,
  },
  s02: {
    title: "Tool Use",
    subtitle: "Add a Tool, Add Just One Line",
    coreAddition: "Tool dispatch map",
    keyInsight: "The loop stays stable while capabilities register into a dispatch table.",
    layer: "tools",
    prevVersion: "s01",
  },
  s03: {
    title: "Permission",
    subtitle: "Check Permissions Before Execution",
    coreAddition: "Permission gate",
    keyInsight: "Dangerous actions need a harness decision point before the shell runs.",
    layer: "tools",
    prevVersion: "s02",
  },
  s04: {
    title: "Hooks",
    subtitle: "Hang on the Loop, Don't Write into It",
    coreAddition: "Lifecycle hooks",
    keyInsight: "Cross-cutting behavior belongs around the loop, not tangled inside it.",
    layer: "tools",
    prevVersion: "s03",
  },
  s05: {
    title: "TodoWrite",
    subtitle: "An Agent Without a Plan Drifts Off Course",
    coreAddition: "Todo manager",
    keyInsight: "Explicit plans keep long-running work visible and correctable.",
    layer: "planning",
    prevVersion: "s04",
  },
  s06: {
    title: "Subagent",
    subtitle: "Break Large Tasks into Small Ones with Clean Context",
    coreAddition: "Isolated subtask context",
    keyInsight: "Subagents give each subtask a clean message history while preserving the main thread.",
    layer: "planning",
    prevVersion: "s05",
  },
  s07: {
    title: "Skill Loading",
    subtitle: "Load Only When Needed",
    coreAddition: "On-demand skill loader",
    keyInsight: "Inject specialized knowledge only when the task actually needs it.",
    layer: "planning",
    prevVersion: "s06",
  },
  s08: {
    title: "Context Compact",
    subtitle: "Context Will Fill Up",
    coreAddition: "Context compaction",
    keyInsight: "Compression keeps the conversation usable when the context window gets crowded.",
    layer: "memory",
    prevVersion: "s07",
  },
  s09: {
    title: "Memory",
    subtitle: "Keep a Layer That Doesn't Lose Details",
    coreAddition: "Durable memory layer",
    keyInsight: "Some facts should survive summarization and future sessions.",
    layer: "memory",
    prevVersion: "s08",
  },
  s10: {
    title: "Context Assembly",
    subtitle: "Build Model Input from Runtime State",
    coreAddition: "Runtime context assembly",
    keyInsight: "Stable instructions and dynamic state should be assembled deliberately at the model boundary.",
    layer: "planning",
    prevVersion: "s09",
  },
  s11: {
    title: "Error Recovery",
    subtitle: "Errors Are the Start of a Retry",
    coreAddition: "Retry strategy",
    keyInsight: "A robust harness classifies failures and decides what kind of retry is worthwhile.",
    layer: "planning",
    prevVersion: "s10",
  },
  s12: {
    title: "Task System",
    subtitle: "Break Big Goals into Small Tasks",
    coreAddition: "Task board",
    keyInsight: "A task graph turns vague goals into ordered, observable work.",
    layer: "collaboration",
    prevVersion: "s11",
  },
  s13: {
    title: "Background Tasks",
    subtitle: "Slow Operations Go to the Background",
    coreAddition: "Background execution",
    keyInsight: "The agent can keep reasoning while slow work completes elsewhere.",
    layer: "concurrency",
    prevVersion: "s12",
  },
  s14: {
    title: "Cron Scheduler",
    subtitle: "Producing Work on a Schedule",
    coreAddition: "Scheduled task creation",
    keyInsight: "Recurring work should be created by the harness, not remembered by the model.",
    layer: "concurrency",
    prevVersion: "s13",
  },
  s15: {
    title: "Agent Team Runtime",
    subtitle: "Persistent Teammates, Atomic Claims, Task-Bound Worktrees",
    coreAddition: "Team runtime with task-bound worktrees",
    keyInsight: "Persistent teammates can reliably discover and execute parallel work when the runtime owns messaging, atomic claims, and task-bound working directories.",
    layer: "collaboration",
    prevVersion: "s14",
  },
  s16: {
    title: "MCP Tools",
    subtitle: "External Tools, Standard Protocol",
    coreAddition: "MCP tool bridge",
    keyInsight: "External services can become agent tools through a standard discovery and call protocol.",
    layer: "collaboration",
    prevVersion: "s15",
  },
  s17: {
    title: "Integrated Harness",
    subtitle: "Many Mechanisms, One Loop",
    coreAddition: "Integrated harness",
    keyInsight: "The integrated harness is still one loop, surrounded by the systems introduced across the course.",
    layer: "collaboration",
    prevVersion: "s16",
  },
  s18: {
    title: "Workflow Runtime",
    subtitle: "Scripts Own Fixed Orchestration",
    coreAddition: "Resumable workflow runtime",
    keyInsight: "When orchestration has a fixed shape, code can make it parallel, deterministic, and resumable.",
    layer: "concurrency",
    prevVersion: "s17",
  },
  s19: {
    title: "Goal Loop",
    subtitle: "Independent Evaluation Decides When to Stop",
    coreAddition: "Goal completion gate",
    keyInsight: "A durable goal keeps the loop working until an independent evaluator finds the completion condition satisfied in the conversation.",
    layer: "planning",
    prevVersion: "s18",
  },
};

export const LAYERS = [
  {
    id: "tools" as const,
    label: "Tools & Execution",
    color: "#3B82F6",
    versions: ["s01", "s02", "s03", "s04"],
  },
  {
    id: "planning" as const,
    label: "Planning & Control",
    color: "#10B981",
    versions: ["s05", "s06", "s07", "s10", "s11", "s19"],
  },
  {
    id: "memory" as const,
    label: "Memory Management",
    color: "#8B5CF6",
    versions: ["s08", "s09"],
  },
  {
    id: "concurrency" as const,
    label: "Concurrency & Scheduling",
    color: "#F59E0B",
    versions: ["s13", "s14", "s18"],
  },
  {
    id: "collaboration" as const,
    label: "Multi-Agent Platform",
    color: "#EF4444",
    versions: ["s12", "s15", "s16", "s17"],
  },
] as const;
