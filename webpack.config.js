const path = require('path');
const {CleanWebpackPlugin} = require('clean-webpack-plugin');
const {WebpackManifestPlugin} = require('webpack-manifest-plugin');
const TerserPlugin = require('terser-webpack-plugin');
const options = {};


module.exports = {
    entry: {
        main: './static/js/OrganizationList.tsx',
        jobsearch: './static/js/JobSearchChat.tsx',
        jobmatch: './static/js/JobMatchPanel.tsx',
    },
    output: {
        path: path.resolve(__dirname, 'static/dist'),
        filename: '[name].[contenthash].js',
    },
    module: {
        rules: [
            {
                test: /\.tsx?$/,
                exclude: /node_modules/,
                use: 'ts-loader',
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