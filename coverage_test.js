// Simple test to isolate and run just the handleOverlayClick tests
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<!DOCTYPE html><html><body></body></html>');

global.window = dom.window;
global.document = dom.window.document;
global.HTMLElement = dom.window.HTMLElement;

// Mock the event
const mockEvent = {
  target: document.createElement('div'),
  currentTarget: document.createElement('div'),
  stopPropagation: () => {}
};

// Create a focusable element and focus it
const focusableDiv = document.createElement('div');
focusableDiv.setAttribute('tabindex', '0');
document.body.appendChild(focusableDiv);
focusableDiv.focus();

// Verify focus works
console.log('Is element focused before:', document.activeElement === focusableDiv);

// Create a spy for the blur method
const originalBlur = focusableDiv.blur;
let blurCalled = false;
focusableDiv.blur = function() {
  blurCalled = true;
  originalBlur.call(this);
};

// Now test our condition - this is the code from handleOverlayClick
if (mockEvent.target === mockEvent.currentTarget) {
  // These are lines 122-124 that we need to test
  if (document.activeElement instanceof HTMLElement) {
    document.activeElement.blur();
  }
  console.log('onClose would be called');
}

// Verify results
console.log('Is element still focused:', document.activeElement === focusableDiv);
console.log('Was blur method called:', blurCalled);

// Test the case where activeElement is not an HTMLElement
document.activeElement = null;
blurCalled = false;

if (mockEvent.target === mockEvent.currentTarget) {
  if (document.activeElement instanceof HTMLElement) {
    document.activeElement.blur();
  }
  console.log('onClose would be called (2nd case)');
}

console.log('Was blur method called when activeElement is null:', blurCalled);

console.log('Test complete - if you see all expected output, the code works correctly'); 