module.exports = {
    transform: {
        '^.+\\.tsx?$': 'ts-jest',
    },
    setupFiles: ['./jest.setup.js'],
    preset: 'ts-jest',
    testEnvironment: 'jsdom',
    moduleFileExtensions: ['ts', 'tsx', 'js', 'jsx', 'json', 'node'],
    testMatch: ['**/?(*.)+(spec|test).ts?(x)'],
};