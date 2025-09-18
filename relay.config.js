module.exports = {
    // ...
    // Configuration options accepted by the `relay-compiler` command-line tool and `babel-plugin-relay`.
    src: "./src",
    language: "typescript", // "javascript" | "typescript" | "flow"
    schema: "./src/schema.gql",
    excludes: ["**/node_modules/**", "**/__mocks__/**", "**/__generated__/**"],
  }