/**
 * What an extension's page imports to reach igni: `connect()`, and the
 * types of what it hands back. The wire format (`protocol`) and the
 * design tokens (`tokens`) are subpaths, because the host imports them
 * too and an extension rarely needs them directly.
 */
export * from "./bridge";
export * from "./hostModel";
export * from "./hostModel.generated";
