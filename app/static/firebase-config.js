// Firebase web config. NOT a secret - these values identify the
// project to browsers and are visible in any web app's source. The
// secret is the service account JSON on the server, which is
// gitignored.
//
// Replace the placeholders with the values from:
//   Firebase console -> Project settings -> General -> Your apps
//   Firebase console -> Project settings -> Cloud Messaging -> Web Push certificates

const firebaseConfig = {
  apiKey: "",
  authDomain: "",
  projectId: "",
  messagingSenderId: "",
  appId: "",
};

// The "Web Push certificate" key pair, same Cloud Messaging page.
self.VAPID_KEY = "";
