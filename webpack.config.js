const path = require('path');
const {CleanWebpackPlugin} = require('clean-webpack-plugin');
const {WebpackManifestPlugin} = require('webpack-manifest-plugin');
const options = {};


module.exports = {
    entry: './static/js/OrganizationList.tsx',
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
        minimize: false
    },
    plugins: [
        new CleanWebpackPlugin(options),
        new WebpackManifestPlugin({
            publicPath: ''
        }),
    ]
};