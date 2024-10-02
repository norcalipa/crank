module.exports = {
    transform: {
        "^.+\\.jsx?$": "babel-jest"
    },
    testEnvironment: 'jsdom',
    setupFiles: ['./jest.setup.js'],
};