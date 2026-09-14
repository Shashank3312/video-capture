// Firebase web config. NOT a secret - these values identify the
// project to browsers and are visible in the source of any web app
// that uses Firebase. The secret is the service account JSON on the
// server, which is gitignored.
//
// From: Firebase console -> Project settings -> General -> Your apps

const firebaseConfig = {
  apiKey: "AIzaSyBpMyerzlCf0Q1WnZc1QP8T8Vde1OIz4H4",
  authDomain: "notify-moment.firebaseapp.com",
  projectId: "notify-moment",
  storageBucket: "notify-moment.firebasestorage.app",
  messagingSenderId: "733450390401",
  appId: "1:733450390401:web:a7a3c30c0285893519f680",
};

// The "Web Push certificate" key pair, from:
//   Project settings -> Cloud Messaging -> Web Push certificates
// Without this the browser can't be issued a push token. This is the
// PUBLIC half of the pair - it ships to every browser that loads the
// page, so it isn't a secret either; Firebase keeps the private half.
self.VAPID_KEY =
  "BNroOiy1_ppOirZ44BaR5ndjuDFHCym8yr0r0JjE1wRcUD7wWN2pHiBBLQBtDRKZHmyGx5IxAmMGClAmCOVHoMk";
