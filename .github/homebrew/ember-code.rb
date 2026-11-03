class EmberCode < Formula
  include Language::Python::Virtualenv

  desc "Terminal-based AI coding assistant"
  homepage "https://github.com/ignite-ember/igni"
  url "https://files.pythonhosted.org/packages/source/e/ember-code/ember_code-0.1.0.tar.gz"
  sha256 "PLACEHOLDER"
  license "MIT"

  depends_on "python@3.12"

  def install
    virtualenv_install_with_resources
  end

  test do
    assert_match "ember-code", shell_output("#{bin}/ignite-ember --help 2>&1", 0)
  end
end
