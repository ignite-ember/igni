"""The documented skill-argument tokens each have to survive the others.

``$ARGUMENTS``, ``$ARGUMENTS[i]`` and ``$N`` (docs/SKILLS.md) used to be
three sequential ``str.replace`` rounds, which made two of them
unreachable: the bare ``$ARGUMENTS`` replace ran first and ate the
prefix of ``$ARGUMENTS[0]``, and ascending ``$1``..``$N`` replaces
rewrote the ``$1`` inside ``$10``. Plus the bare ``/`` that used to
IndexError out of the skill router.
"""

from ember_code.core.skills.loader import SkillPool
from ember_code.core.skills.parser import SkillDefinition


class TestIndexedArguments:
    def test_indexed_form_survives_the_bare_form(self):
        defn = SkillDefinition(
            name="t",
            body="A=$ARGUMENTS | first=$1 | idx0=$ARGUMENTS[0] | idx1=$ARGUMENTS[1]",
        )
        assert defn.render("alpha beta") == ("A=alpha beta | first=alpha | idx0=alpha | idx1=beta")

    def test_indexed_alone(self):
        defn = SkillDefinition(name="t", body="$ARGUMENTS[1]/$ARGUMENTS[0]")
        assert defn.render("src dst") == "dst/src"

    def test_out_of_range_index_is_left_as_written(self):
        defn = SkillDefinition(name="t", body="a=$ARGUMENTS[0] b=$ARGUMENTS[4] c=$9")
        assert defn.render("only") == "a=only b=$ARGUMENTS[4] c=$9"

    def test_dollar_zero_is_not_an_argument(self):
        defn = SkillDefinition(name="t", body="$0 $1")
        assert defn.render("alpha") == "$0 alpha"


class TestPositionalArguments:
    def test_tenth_argument_is_not_clobbered_by_the_first(self):
        words = "one two three four five six seven eight nine ten eleven"
        defn = SkillDefinition(name="t", body="$1 $10 $11")
        # Ascending str.replace made this "one0 one1": $1 matched the
        # prefix of $10 before $10 was ever looked at.
        assert defn.render(words) == "one ten eleven"

    def test_substituted_text_is_not_rescanned(self):
        defn = SkillDefinition(name="t", body="$1 $2")
        assert defn.render("$2 beta") == "$2 beta"

    def test_empty_arguments_still_blanks_the_bare_token(self):
        defn = SkillDefinition(name="t", body="Do $ARGUMENTS now.")
        assert defn.render("") == "Do  now."

    def test_skill_dir_token_is_untouched_by_argument_substitution(self, tmp_path):
        defn = SkillDefinition(
            name="t",
            body="${IGNI_SKILL_DIR}/x $1",
            source_dir=tmp_path,
        )
        assert defn.render("alpha") == f"{tmp_path}/x alpha"


class TestBareSlash:
    def test_bare_slash_does_not_raise(self):
        pool = SkillPool()
        for text in ("/", "/ ", "/  ", "  /  "):
            assert pool.match_user_command(text) is None
