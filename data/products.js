// Moceol — shop catalogue.
// Plain .js (not .json) so it loads with a <script> tag and works without a server.
// The Gumroad API is never called from the browser (it would expose a secret),
// so real product info is kept here by hand.

window.PRODUCTS = [
    {
        title: "Clarity of Mind",
        price: "$5",
        coverUrl: "images/clarity-of-mind-cover.svg",
        link: "https://oetim.gumroad.com/l/eyurqz",
        comingSoon: false
    },
    {
        comingSoon: true
    },
    {
        comingSoon: true
    }
];
