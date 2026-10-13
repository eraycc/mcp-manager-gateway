// Format explicitly requested full single-item JSON away from the UI thread.
self.onmessage=event=>self.postMessage(JSON.stringify(event.data,null,2));
