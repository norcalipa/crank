const path = require('path');
const {CleanWebpackPlugin} = require('clean-webpack-plugin');
const {WebpackManifestPlugin} = require('webpack-manifest-plugin');
const options = {};


module.exports = {
    entry: './static/js/OrganizationList.js',
    output: {
        path: path.resolve(__dirname, 'static/dist'),
        filename: '[name].[contenthash].js',
    },
    module: {
        rules: [
            {
                test: /\.js$/,
                exclude: /node_modules/,
                use: {
                    loader: 'babel-loader',
                    options: {
                        presets: ['@babel/preset-env', '@babel/preset-react'],
                    },
                },
            },
        ],
    },
    mode: 'production',
    optimization: {
        minimize: false
    },
    plugins: [
        new CleanWebpackPlugin(options),
        new WebpackManifestPlugin({
            publicPath: 'dist'
        }),
    ]
};