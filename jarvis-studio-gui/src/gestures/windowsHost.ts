import { reorderCards, type Action, type GestureConfig } from "./engine";
import type { GestureReply } from "./request";
interface Options {
  listen: () => unknown;
  pttStart: () => unknown;
  finishListen: () => unknown;
  stopSpeech: () => unknown;
  request: (
    action: string,
    data?: Record<string, unknown>,
  ) => Promise<GestureReply>;
  order: Record<string, string[]>;
  sendScreen: (patch: unknown) => unknown;
  routines: Array<{ id: string; name: string }>;
  runRoutine: (id: string) => boolean;
  ready: boolean;
  armControl: () => unknown;
}
export function windowsGestureHost(options: Options) {
  return {
    ...options,
    cancelListen: options.stopSpeech,
    speak: (text: string) => options.request("read_aloud", { text }),
    reorder(from: string, to: string) {
      const order = reorderCards(options.order, from, to);
      if (order !== options.order) options.sendScreen({ order });
    },
    async perform(
      action: Action,
      cfg: GestureConfig,
      camera: { photo: () => Promise<GestureReply> },
    ) {
      if (action === "photo.capture") return camera.photo();
      if (action === "routine.run") {
        if (!options.ready)
          return {
            ok: false,
            summary: "Wait for JARVIS to finish before launching a routine.",
          };
        const routine = options.routines.find(
          (r) => r.name === cfg.routineName,
        );
        if (!routine)
          return {
            ok: false,
            summary: "Choose an existing saved routine in gesture settings.",
          };
        const sent = options.runRoutine(routine.id);
        return {
          ok: sent,
          summary: sent
            ? "Requested the routine; its results and approvals appear in the conversation."
            : "The PC is not connected.",
        };
      }
      return options.request(
        action,
        action === "timer.start" ? { seconds: cfg.timerSeconds } : {},
      );
    },
  };
}
