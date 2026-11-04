import { act, create } from "react-test-renderer";
import { expect, it, vi } from "vitest";
import Onboarding from "./Onboarding";
import SetupProgress from "./SetupProgress";
const textOf = (node) => typeof node === "string" ? node : node.children.map(textOf).join("");

it("lets a user explore without saving fake credentials or claiming AI setup is complete", async () => {
  const onExplore=vi.fn(), onSave=vi.fn(); let view;
  await act(async()=>{view=create(<Onboarding sysInfo={{storage_dir:"C:/Jarvis"}} onSave={onSave} onExplore={onExplore}/>)});
  const buttons=view.root.findAllByType("button");
  expect(buttons.find((node)=>textOf(node).includes("Start JARVIS")).props.disabled).toBe(true);
  await act(async()=>buttons.find((node)=>textOf(node)==="Explore without an AI key").props.onClick());
  expect(onExplore).toHaveBeenCalledOnce(); expect(onSave).not.toHaveBeenCalled();
  await act(async()=>view.unmount());
});

it("keeps optional setup failures honest while allowing available features", async () => {
  const onRepair=vi.fn();let view;
  const progress={complete:true,total:1,order:["browser"],items:{browser:{label:"Browser",status:"error",error:"Download unavailable"}}};
  await act(async()=>{view=create(<SetupProgress progress={progress} onRepair={onRepair}/>)});
  expect(JSON.stringify(view.toJSON())).toContain("Download unavailable");
  const button=view.root.findAllByType("button").find((node)=>textOf(node)==="Continue with available features");
  await act(async()=>button.props.onClick());
  expect(view.toJSON()).toBeNull();expect(onRepair).not.toHaveBeenCalled();
  expect(progress.items.browser.status).toBe("error");
  await act(async()=>view.unmount());
});
