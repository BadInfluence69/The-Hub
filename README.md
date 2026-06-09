# The Hub Verson 3.1

An open-source, self-hosted video platform powered by Python that allows you to stream YouTube videos completely ad-free across your devices. The Hub acts as a local server, scraping public YouTube content and offering a completely independent ecosystem free from Google trackers, ads, and restrictions. 

Whether you want to watch content on your computer, phone, tablet, or a Roku streaming device, The Hub automates the heavy lifting so you can get a private streaming server up and running instantly.

---

### Key Features

* **Ad-Free Streaming:** Watch your favorite public YouTube videos and live streams with zero commercial interruptions.
* **Multi-Platform Support:** Fully responsive local web interface for PCs, tablets, and phones, plus an included Roku application endpoint.
* **Zero-Configuration Deployment:** The Python backend is fully automated. On its first run, it dynamically generates all necessary file structures, folder layouts, and a lightweight local database file—no Apache, MySQL, or complex database configuration required.
* **Local Interactive Ecosystem:** Sign into a local account generated right on your server. Like, dislike, leave comments, and participate in feature suggestion voting entirely within your self-hosted instance.
* **Hybrid Data Fetching:** Operates seamlessly out of the box using public scraping methods. If you prefer enhanced performance, you can optionally plug in your own YouTube Data API key (not required).

---

### Project Limitations & Workarounds

Because this project bypasses traditional Google OAuth tracking, it cannot directly modify data on official Google servers. 

* **No Official Google Sign-In:** You cannot log into your official YouTube/Google account. Instead, use the automated local account portal to manage your watch experience.
* **Local Comments Only:** You cannot leave public comments directly on YouTube.com. All comments, likes, and dislikes are stored and interacted with internally via your local database file.
* **No Paid Content:** Rental movies, premium subscription content, and paid YouTube features are not supported.

---

### Component Status

#### Local Python Web Server
* **Status:** Fully Functional & Stable
* This handles the scraping, endpoint routing, local account system, and initial database generation.

#### Roku Application
* **Status:** Operational (Needs TLC)
* The repository includes a Roku application that communicates directly with the Python server's dedicated Roku endpoint. 
* **Note:** The Roku app is not fully finished and requires some "Tender Loving Care" (TLC), but it is operational and can be successfully sideloaded onto your Roku TV or streaming stick to fetch and stream content from your local server.

---

### Deployment & Setup

Getting your instance of The Hub running is designed to be entirely self-automated:

1. **Download the Project:** Download the repository ZIP file from GitHub and extract it, or clone the project directly to your computer or server.
2. **Run the Script:** Execute the main Python script. 
3. **Automated Initialization:** The script will automatically build out the required folder structure and initialize the local database file on its very first launch.
4. **Access the Hub:** Open your local browser to the hosted web address provided by the script. The landing page features a detailed rundown explaining the project, its purpose, and its architecture.
5. **Roku Sideloading (Optional):** Sideload the included Roku application folder onto your Roku device and point it toward your Python server's local IP address.

#### Configuring Subscriptions
By default, the "My Subscriptions" tab is pre-loaded with the creator's YouTube channel subscriptions. You have complete control over this data:
* Leave them exactly as they are.
* Append your own favorite channels alongside them.
* Completely wipe them out and replace them entirely with your personal subscription list inside your local instance files.

FFmeg http://72.51.249.70/TheHub/ffmpeg.zip

YU-DLP is a modded verson compiled from source code 

---

### License & Contributing

The Hub is proudly licensed under the **Apache License 2.0**. 

We explicitly invite open-source developers to contribute, modify, and enhance this code base to help it evolve! By utilizing, modifying, or contributing to this project, you agree to the following terms:

* **Keep the Brand:** The project name must remain **The Hub**.
* **Attribution:** You must preserve original credits to the creator where credit is due.
* **Upstream Contributions:** If you modify, optimize, or build new features for your personal deployment, you are expected to submit a Pull Request to contribute those enhancements back to this primary repository so the entire community can benefit.

To start contributing, fork the repository, make your changes, and submit a pull request!
