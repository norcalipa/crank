// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
// Dedicated webpack build for the Playwright E2E fixture. Produces a stable
// `static/dist/e2e.js` bundle (no content hash) so the static fixture pages can
// reference it without reading the webpack manifest. Kept separate from the
// production build so the site manifest stays unchanged.
const path = require('path');

module.exports = {
    entry: {
        e2e: './static/js/e2e/job-search-chat.e2e.tsx',
    },
    output: {
        path: path.resolve(__dirname, 'static/dist'),
        filename: 'e2e.js',
        publicPath: '/static/dist/',
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
    mode: 'development',
    devtool: false,
};
