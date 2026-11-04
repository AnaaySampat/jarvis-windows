import { Icon as BaseIcon } from "../hud/icons";
export function Icon({ name, ...props }) { return <BaseIcon name={name === "screen" ? "monitor" : name} {...props} />; }
