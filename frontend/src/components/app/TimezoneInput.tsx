import { useId, type ComponentProps } from "react";

import { Input } from "@/components/ui/input";

const TIMEZONES: string[] =
  typeof Intl.supportedValuesOf === "function" ? Intl.supportedValuesOf("timeZone") : ["UTC"];

/** Free-text input that suggests the browser's IANA time zones from its own datalist. */
export function TimezoneInput(props: Omit<ComponentProps<typeof Input>, "list">) {
  const listId = useId();
  return (
    <>
      <Input {...props} list={listId} />
      <datalist id={listId}>
        {TIMEZONES.map((tz) => (
          <option key={tz} value={tz} />
        ))}
      </datalist>
    </>
  );
}
