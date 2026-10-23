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
 */

export {
  Actions,
  Badge,
  Button,
  type ButtonProps,
  type ButtonSize,
  type ButtonVariant,
  Card,
  Chip,
  Dialog,
  type DialogProps,
  Empty,
  Input,
  type InputProps,
  Item,
  List,
  Row,
  Table,
  type Tone,
  Toolbar,
} from "./primitives";

export {
  MarkdownInline,
  closeOpenMarks,
  dropEchoedTitle,
} from "./MarkdownInline";
export { MenuIcon, type MenuIconName, type MenuIconProps } from "./MenuIcon";
export { Monogram, hexOf, hueOf, initialsOf } from "./Monogram";
export { OverflowMenu, type OverflowAction } from "./OverflowMenu";
export { MermaidBlock, SpecBlock, fenceInfo, loadMermaid } from "./RichFences";
export { Skeleton } from "./Skeleton";
export { Tooltip, type TooltipProps } from "./Tooltip";
export {
  applyVisualizationDelta,
  parsePartialSpec,
  type VisualizationDeltaEvent,
} from "./visualizationStream";
