/**
 * @igni/ui-react — the components an extension is allowed to build on.
 *
 * This list is not a guess at what an extension might want. It is
 * what the first two extensions actually import. CodeIndex needs
 * none of it — its only app dependency is the client type, which is
 * why porting its panel behind the bridge cost one line. Artifacts
 * is 7,737 lines and imports exactly seven of the app's components,
 * measured rather than estimated.
 *
 * Seven was never the whole floor, though. Artifacts also writes
 * `className="btn btn-primary"` by hand, and so would every
 * extension — the primitives existed as CSS and not as an API, which
 * is no autocomplete, no way to learn a variant name short of
 * reading `theme.css`, and nothing to stop `btn-primery` shipping as
 * an invisible bug. `./primitives` closes that: thin typed wrappers
 * over classes the app already uses.
 *
 * They are React, not web components, because six of the seven are
 * worse as web components: `MarkdownInline` and `RichFences` are a
 * react-markdown pipeline with remark and rehype plugins, and
 * `Tooltip` and `OverflowMenu` are positioning behaviour that wants
 * the light DOM. Rewriting those inside a shadow root would mean
 * maintaining a second implementation of something the app already
 * has — the drift this repository has been bitten by twice.
 *
 * An extension runs in its own frame and bundles its own React, so
 * consuming a React package costs it nothing it was not already
 * paying.
 *
 * **Everything exported here is under the compatibility floor.**
 * Removing one, renaming it, or changing a prop's meaning is a
 * breaking change under EXTENSIBILITY.md §8. Anything in the app
 * that is not exported here stays ours to change — which is why this
 * file is short and should be argued over before it grows.
 *
 * ## The two rules an export has to pass
 *
 * **It must render outside this repository.** A component whose CSS
 * lives only in ``theme.css`` is unstyled in the one place it is
 * promised for, because an extension runs on its own origin under
 * ``default-src 'self'`` and cannot link ours. ``styled.test.ts``
 * checks every class named here against ``clients/ui/igni-ui.css``
 * and fails on the first one that is missing. This was not a
 * hypothetical: seven components shipped under the floor naming
 * thirty-two classes that no published stylesheet defined, and the
 * reference extension did not catch it because it happens to import
 * only from ``primitives``, which is the half that was covered.
 *
 * **It must not drag the app behind it.** ``boundary.test.ts``
 * fails on an import — static or dynamic — that leaves this
 * directory for anywhere but ``react`` and the published
 * dependencies. ``SpecBlock`` left the floor for this reason: it
 * loaded ``../components/JsonRenderView``, a path that does not
 * exist in a published package, through a dynamic import that no
 * compiler would flag. ``visualizationStream`` left for the same
 * reason — it types its return as the app's ``ChatItem``, which
 * would have put the chat model under the floor by transitivity.
 *
 * Helpers that only their own tests imported — ``closeOpenMarks``,
 * ``dropEchoedTitle``, ``hexOf``, ``hueOf``, ``initialsOf`` — are
 * gone from here too. They were exported to be tested, and tests in
 * this repository can import them from their own module. Putting
 * them here promised them to extensions for two releases.
 */

export {
  Actions,
  Badge,
  Button,
  type ButtonProps,
  type ButtonSize,
  type ButtonVariant,
  type BadgeProps,
  Card,
  Chip,
  type ChipProps,
  Dialog,
  type DialogProps,
  Empty,
  type EmptyProps,
  Input,
  type InputProps,
  Item,
  List,
  Row,
  Checkbox,
  type CheckboxProps,
  Code,
  Divider,
  Field,
  type FieldProps,
  Heading,
  type HeadingProps,
  Progress,
  type ProgressProps,
  Radio,
  type RadioProps,
  Select,
  type SelectProps,
  Spinner,
  type SpinnerProps,
  Switch,
  type SwitchProps,
  Table,
  TabPanel,
  type TabPanelProps,
  Tabs,
  type TabsProps,
  Text,
  type TextProps,
  type TextTone,
  Textarea,
  type TextareaProps,
  type Tone,
  Toolbar,
} from "./primitives";

export { AppShell, type AppShellProps, Scroll, type ScrollProps } from "./Shell";
export {
  SortableTh,
  type SortableThProps,
  type SortDirection,
  Status,
  type StatusProps,
} from "./DataTable";
export { ErrorBoundary, type ErrorBoundaryProps } from "./ErrorBoundary";
export {
  Banner,
  type BannerProps,
  type Toast,
  type ToastApi,
  ToastHost,
  type ToastOptions,
  useToast,
} from "./Toast";
export {
  type Announcer,
  LiveRegion,
  type LiveRegionProps,
  useAnnounce,
  type Urgency,
} from "./LiveRegion";
export { Markdown, type MarkdownProps } from "./Markdown";
export { MarkdownInline } from "./MarkdownInline";
export { Split, type SplitProps } from "./Split";
export { MenuIcon, type MenuIconName, type MenuIconProps } from "./MenuIcon";
export { Monogram } from "./Monogram";
export { OverflowMenu, type OverflowAction } from "./OverflowMenu";
export { MermaidBlock, fenceInfo, loadMermaid } from "./Mermaid";
export { Skeleton } from "./Skeleton";
export { Tooltip, type TooltipProps } from "./Tooltip";
