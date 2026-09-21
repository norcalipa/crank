const path = require('path');
const {CleanWebpackPlugin} = require('clean-webpack-plugin');
const {WebpackManifestPlugin} = require('webpack-manifest-plugin');
const TerserPlugin = require('terser-webpack-plugin');
const options = {};


module.exports = {
    entry: {
        main: './static/js/main.tsx',
        jobmatch: './static/js/jobmatch.tsx',
    },
    output: {
        path: path.resolve(__dirname, 'static/dist'),
        filename: '[name].[contenthash].js',
        // React.lazy async chunks (issue #472): resolve against Django's
        // static prefix instead of the page URL.
        chunkFilename: '[name].[contenthash].chunk.js',
        publicPath: '/static/dist/',
    },
    module: {
        rules: [
            {
                test: /\.tsx?$/,
                exclude: /node_modules/,
                use: {
                    loader: 'ts-loader',
                    options: {
                        // Webpack needs ES modules to code-split React.lazy's
                        // dynamic import (issue #472); the shared tsconfig
                        // stays commonjs for ts-jest.
                        compilerOptions: {module: 'esnext'},
                        // Test files stay on the shared tsconfig (commonjs)
                        // for ts-jest; webpack only checks what it bundles.
                        onlyCompileBundledFiles: true,
                    },
                },
            },
        ],
    },
    resolve: {
        extensions: ['.tsx', '.ts', '.js'],
    },
    mode: 'production',
    optimization: {
        minimize: true,
        minimizer: [new TerserPlugin()],
    },
    plugins: [
        new CleanWebpackPlugin(options),
        new WebpackManifestPlugin({
            publicPath: ''
        }),
    ]
};