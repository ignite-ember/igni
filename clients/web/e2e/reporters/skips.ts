/**
 * A reporter that will not let this suite report green while quietly
 * not running things.
 *
 * F128. Before this, ``npx playwright test`` ended in "41 passed"
 * while skipping seven tests, and those seven were exactly the ones
 * that prove the app works against a real backend. Two of them were
 * the only coverage of the backend's RPC dispatch table. Nothing in
 * the output said so: Playwright's line reporter prints "7 skipped"
 * on one line and no detail, and CI's step went green.
 *
 * The rule here is not a list of which tests may skip — a list goes
 * stale the moment someone adds a spec. It is: **every skip must be
 * declared by a tag on the test**. A tagged skip is a limitation
 * someone chose and wrote down, and it prints as a reminder. An
 * untagged skip is an accident, and under ``IGNI_E2E_STRICT=1`` it
 * fails the run.
 *
 * Strict mode is on in CI. Locally the run still passes — but it says,
 * every time, what it did not check.
 *
 * As of F129 nothing carries a tag: the three specs that used to need a
 * hand-run seeding harness now seed themselves, so all 48 run. The
 * vocabulary stays because the rule needs somewhere for a future
 * genuinely-manual check to declare itself; it is not a list of
 * exceptions that has to be kept in step with the suite.
 */

import type {
  FullConfig,
  FullResult,
  Reporter,
  Suite,
  TestCase,
  TestResult,
} from "@playwright/test/reporter";

/** Tags that declare a skip as deliberate, with what they mean. */
const DECLARED: Record<string, string> = {
  "@manual": "a by-hand check, not something CI can assert",
  "@needs-baseline":
    "a pixel comparison with no committed baseline for this platform; " +
    "Playwright names baselines per-OS and only the Linux ones are in " +
    "the repository. Run scripts/baselines-linux.sh to compare here",
};

type Skip = {
  title: string;
  file: string;
  line: number;
  reason: string;
  declaredBy: string | null;
};

const BAR = "─".repeat(72);

export default class SkipReporter implements Reporter {
  private skips: Skip[] = [];
  private ran = 0;
  private strict = process.env.IGNI_E2E_STRICT === "1";
  private rootDir = "";

  onBegin(config: FullConfig, _suite: Suite): void {
    this.rootDir = config.rootDir;
  }

  onTestEnd(test: TestCase, result: TestResult): void {
    if (result.status !== "skipped") {
      this.ran += 1;
      return;
    }
    const declaredBy = test.tags.find((t) => t in DECLARED) ?? null;
    this.skips.push({
      title: test.titlePath().slice(3).join(" › ") || test.title,
      file: test.location.file.replace(`${this.rootDir}/`, ""),
      line: test.location.line,
      reason:
        result.annotations?.find((a) => a.type === "skip")?.description ??
        test.annotations.find((a) => a.type === "skip")?.description ??
        "no reason given",
      declaredBy,
    });
  }

  async onEnd(result: FullResult): Promise<{ status?: FullResult["status"] }> {
    if (this.skips.length === 0) {
      // Worth saying out loud: it is the only run that proves the
      // whole suite was reachable on this machine.
      console.log(`\n${BAR}\nEvery test ran. Nothing was skipped.\n${BAR}`);
      return {};
    }

    const undeclared = this.skips.filter((s) => !s.declaredBy);
    const declared = this.skips.filter((s) => s.declaredBy);

    const out: string[] = ["", BAR];
    out.push(
      `${this.ran} tests ran. ${this.skips.length} did NOT — this run does ` +
        `not prove what they cover.`,
    );

    if (declared.length) {
      out.push("", `Declared (${declared.length}) — deliberate, and why:`);
      for (const s of declared) {
        out.push(`  · ${s.title}`);
        out.push(`      ${s.file}:${s.line}  ${s.declaredBy}`);
        out.push(`      ${DECLARED[s.declaredBy!]}`);
      }
    }

    if (undeclared.length) {
      out.push(
        "",
        `Undeclared (${undeclared.length}) — no tag says these are ` +
          `meant to skip:`,
      );
      for (const s of undeclared) {
        out.push(`  ✗ ${s.title}`);
        out.push(`      ${s.file}:${s.line}`);
        out.push(`      reason given: ${s.reason}`);
      }
      out.push(
        "",
        "  Either make them runnable — the backend fixture in",
        "  e2e/fixtures/live-be.ts spawns a real one whenever the project",
        "  venv exists, which is what most of these used to need an",
        "  environment variable for — or tag them with one of:",
        `    ${Object.keys(DECLARED).join("  ")}`,
      );
    }

    out.push(BAR, "");
    console.log(out.join("\n"));

    if (this.strict && undeclared.length) {
      console.log(
        `IGNI_E2E_STRICT=1 and ${undeclared.length} test(s) skipped ` +
          `without a tag. Failing the run.\n`,
      );
      return { status: "failed" };
    }
    if (!this.strict && undeclared.length) {
      console.log(
        "Set IGNI_E2E_STRICT=1 to make an undeclared skip fail, the way " +
          "CI does.\n",
      );
    }
    return { status: result.status };
  }
}
