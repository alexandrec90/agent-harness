import remarkFrontmatter from "remark-frontmatter";
import remarkPresetLintRecommended from "remark-preset-lint-recommended";

export default {
  plugins: [[remarkFrontmatter, ["yaml"]], remarkPresetLintRecommended],
};
