const path = require("path");
const BundleTracker = require("webpack-bundle-tracker");
const { CleanWebpackPlugin } = require("clean-webpack-plugin");
const ESLintPlugin = require("eslint-webpack-plugin");
const ForkTsCheckerWebpackPlugin = require("fork-ts-checker-webpack-plugin");
const MiniCssExtractPlugin = require("mini-css-extract-plugin");

module.exports = {
    entry: {
        frontend: ["./src/index.js"],
    },
    output: {
        path: path.resolve("./frontend/static/frontend/"),
        filename: "[name]-[fullhash].js",
        publicPath: "static/frontend/",
    },
    plugins: [
        new CleanWebpackPlugin(),
        new BundleTracker({
            path: __dirname,
            filename: "./webpack-stats.json",
        }),
        new ESLintPlugin({
            useEslintrc: true,
            failOnWarning: true,
            extensions: ["js", "ts", "jsx", "tsx"],
        }),
        new ForkTsCheckerWebpackPlugin(),
        new MiniCssExtractPlugin({
            filename: "[name]-[fullhash].css",
        }),
    ],
    optimization: {
        splitChunks: {
            chunks: "all",
        },
    },
    performance: {
        hints: false,
        maxEntrypointSize: 512000,
        maxAssetSize: 512000,
    },
    module: {
        rules: [
            {
                test: /\.jsx?$/,
                include: path.resolve(__dirname, "src"),
                exclude: /node_modules/,
                use: [
                    {
                        loader: "swc-loader",

                        options: {
                            jsc: {
                                parser: {
                                    syntax: "ecmascript",
                                    jsx: true,
                                },
                                transform: {
                                    react: {
                                        pragma: "React.createElement",
                                        pragmaFrag: "React.Fragment",
                                        throwIfNamespace: true,
                                        development: false,
                                        useBuiltins: false,
                                    },
                                },
                            },
                        },
                    },
                ],
            },
            {
                test: /\.tsx?$/,
                use: [
                    {
                        loader: "swc-loader",

                        options: {
                            jsc: {
                                parser: {
                                    syntax: "typescript",
                                    tsx: true,
                                },
                                transform: {
                                    react: {
                                        pragma: "React.createElement",
                                        pragmaFrag: "React.Fragment",
                                        throwIfNamespace: true,
                                        development: true,
                                        useBuiltins: true,
                                    },
                                },
                                experimental: {
                                    plugins: [
                                        [
                                            "@swc/plugin-relay",
                                            {
                                                rootDir: __dirname,
                                                artifactDirectory:
                                                    "src/__generated__",
                                                language: "typescript",
                                                eagerEsModules: true,
                                            },
                                        ],
                                    ],
                                },
                            },
                        },
                    },
                ],
                exclude: /node_modules/,
            },
            {
                test: /\.css$/,
                use: ["style-loader", "css-loader"],
            },
            {
                test: /\.less$/i,
                use: [
                    {
                        loader: MiniCssExtractPlugin.loader,
                    },
                    {
                        loader: "css-loader", // translates CSS into CommonJS
                    },
                    {
                        loader: "less-loader", // compiles Less to CSS
                    },
                ],
            },
        ],
    },
    resolve: {
        extensions: ["", ".js", ".jsx", ".ts", ".tsx", ".less"],
    },
};
