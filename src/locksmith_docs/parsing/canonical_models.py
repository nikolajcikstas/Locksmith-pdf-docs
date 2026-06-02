from __future__ import annotations

from difflib import SequenceMatcher


CANONICAL_MODELS: dict[str, list[str]] = {
    "Acura": ["ILX", "Integra", "Legend", "MDX", "NSX", "RDX", "RL", "RLX", "RSX", "SLX", "TL", "TLX", "TSX", "Vigor", "ZDX"],
    "Audi": ["A3", "A4", "A4 / S4", "A5", "A6", "A6 / S6", "A7", "A8", "A8 / S8", "Q3", "Q5", "Q7", "Q8", "R8", "TT"],
    "BMW": ["1 Series", "2 Series", "3 Series", "4 Series", "5 Series", "6 Series", "7 Series", "X1", "X3", "X5", "X6", "Z4"],
    "Buick": ["Century", "Enclave", "Encore", "LaCrosse", "LeSabre", "Lucerne", "Park Avenue", "Regal", "Rendezvous"],
    "Cadillac": ["ATS", "CTS", "DeVille", "DTS", "Eldorado", "Escalade", "Seville", "SRX", "STS", "XTS"],
    "Chevrolet": ["Astro", "Avalanche", "Blazer", "Camaro", "Cobalt", "Corvette", "Equinox", "Express", "Impala", "Malibu", "Metro", "S10", "Silverado", "Sonic", "Spark", "Suburban", "Tahoe", "Tracker", "TrailBlazer"],
    "Chrysler": ["200", "300", "300C", "Aspen", "Concorde", "Crossfire", "Pacifica", "PT Cruiser", "Sebring", "Town & Country", "Voyager"],
    "Dodge": ["Avenger", "Caliber", "Caravan", "Challenger", "Charger", "Dakota", "Dart", "Durango", "Grand Caravan", "Journey", "Magnum", "Neon", "Nitro", "Ram", "Stratus", "Viper"],
    "Ford": ["Blackwood", "Continental", "Crown Victoria", "E Series", "Edge", "Escape", "Expedition", "Explorer", "Explorer Sport Trac", "F150", "F250", "F350", "Fiesta", "Focus", "Freestar", "Fusion", "Mariner", "Mountaineer", "Mustang", "Navigator", "Taurus", "Town Car", "Windstar"],
    "GMC": ["Acadia", "Canyon", "Envoy", "Safari", "Savana", "Sierra", "Sonoma", "Suburban", "Terrain", "Yukon"],
    "Honda": ["Accord", "Civic", "Clarity", "Clarity Electric", "Clarity Fuel Cell", "Clarity Plug-In", "CR-V", "CR-Z", "Crosstour", "Element", "Fit", "Insight", "Odyssey", "Passport", "Pilot", "Prelude", "Ridgeline", "S2000"],
    "Infiniti": ["EX35", "FX35", "FX45", "G35", "G37", "I30", "I35", "JX35", "M35", "M45", "Q45", "Q50", "Q60", "QX4", "QX50", "QX55", "QX56", "QX60", "QX80"],
    "Isuzu": ["Amigo", "Ascender", "Axiom", "Hombre", "Impulse", "Oasis", "Pickup", "Rodeo", "Rodeo Sport", "Trooper"],
    "Jeep": ["Cherokee", "Commander", "Compass", "Grand Cherokee", "Liberty", "Patriot", "Renegade", "Wrangler"],
    "Kia": ["Amanti", "Cadenza", "Forte", "K5", "Optima", "Optima Hybrid", "Rio", "Sedona", "Sorento", "Soul", "Sportage", "Stinger", "Telluride"],
    "Land Rover": ["Defender", "Discovery", "Evoque", "Freelander", "LR2", "LR3", "LR4", "Range Rover", "Range Rover Sport", "Range Rover Evoque"],
    "Lexus": ["CT 200h", "ES 300", "ES 330", "ES 350", "GS 300", "GS 350", "GX 460", "GX 470", "IS 250", "IS 300", "IS 350", "LS 400", "LS 430", "LS 460", "LS 600h", "LX 470", "LX 570", "NX 200t", "NX 300", "NX 300h", "RC 200t", "RC 350", "RX 300", "RX 330", "RX 350", "RX 400h", "RX 450h", "SC 430"],
    "Mazda": ["2", "3", "5", "6", "CX-3", "CX-5", "CX-7", "CX-9", "Miata", "MPV", "MX-5 Miata", "Protege", "RX-8", "Tribute"],
    "Mercedes-Benz": ["190", "240D", "280S", "300CD", "300D", "300TD", "380SL", "420SL", "450SL", "500SL", "560SL", "600SL", "A-Class", "C-Class", "CLA", "CLS", "E-Class", "G-Class", "GL", "GLA", "GLC", "GLE", "GLK", "ML", "S-Class", "SL", "SLK", "Sprinter"],
    "Mini": ["Clubman", "Cooper", "Countryman", "Paceman"],
    "Mitsubishi": ["Cordia", "Diamante", "Eclipse", "Eclipse Cross", "Endeavor", "Fuso", "Galant", "i-MiEV", "Lancer", "Mirage", "Montero", "Outlander", "Pickup", "Raider", "Sigma", "Starion", "Tredia", "Van"],
    "Nissan": ["200SX", "240SX", "260Z", "280Z", "Armada", "Frontier", "Kicks", "Leaf", "Maxima", "Murano", "Pathfinder", "Pickup", "Pulsar", "Quest", "Rogue", "Sentra", "Titan", "Versa", "Xterra"],
    "Oldsmobile": ["Achieva", "Aurora", "Bravada", "Cutlass", "Cutlass Supreme", "Intrigue", "Silhouette"],
    "Pontiac": ["Bonneville", "Firebird", "Grand Am", "Grand Prix", "GTO", "Solstice", "Sunfire", "Torrent", "Vibe"],
    "Subaru": ["Baja", "Forester", "Impreza", "Impreza STi", "Legacy", "Outback", "Tribeca", "WRX"],
    "Suzuki": ["Aerio", "Equator", "Esteem", "Forenza", "Grand Vitara", "Samurai", "Sidekick", "Swift", "Vitara", "XL-7"],
    "Toyota": ["4Runner", "Avalon", "Camry", "Corolla", "FJ Cruiser", "Highlander", "Land Cruiser", "Matrix", "Prius", "RAV4", "Sequoia", "Sienna", "Tacoma", "Tundra", "Venza", "Yaris"],
    "Volkswagen": ["Beetle", "Cabriolet", "CC", "Corrado", "Eos", "Eurovan", "Golf", "Golf / GTI", "GTI", "Jetta", "Passat", "Phaeton", "Rabbit", "Routan", "Tiguan", "Touareg"],
    "Porsche": ["911", "928", "718 Boxster", "718 Cayman", "Boxster", "Cayman", "Cayenne", "Macan", "Panamera", "Taycan"],
    "Saab": ["9-2X", "9-3", "9-5", "9-7X", "900", "9000"],
    "Saturn": ["Aura", "Ion", "L-Series", "Outlook", "Relay", "Sky", "Vue"],
    "Volvo": ["C30", "C70", "S40", "S60", "S70", "S80", "S90", "V40", "V50", "V70", "V90", "XC60", "XC70", "XC90"],
    "Fiat": ["124 Spider", "500", "500e", "500L", "500X", "Strada"],
    "Alfa Romeo": ["4C", "Giulia", "Stelvio"],
    "Jaguar": ["E-Pace", "F-Pace", "F-Type", "I-Pace", "S-Type", "Vanden Plas", "X-Type", "XE", "XF", "XJ", "XJR", "XK"],
    "Lincoln": ["Aviator", "Blackwood", "Continental", "LS", "MKC", "MKS", "MKT", "MKX", "MKZ", "Navigator", "Town Car", "Zephyr"],
    "Mercury": ["Cougar", "Grand Marquis", "Mariner", "Milan", "Montego", "Mountaineer", "Sable"],
}


def canonicalize_model(make: str, model: str) -> tuple[str, float]:
    choices = CANONICAL_MODELS.get(make, [])
    if not choices:
        return model, 0.0
    normalized = compact(model)
    best = model
    best_score = 0.0
    for choice in choices:
        score = SequenceMatcher(None, normalized, compact(choice)).ratio()
        if normalized in compact(choice) or compact(choice) in normalized:
            score = max(score, 0.9)
        if score > best_score:
            best = choice
            best_score = score
    return (best, best_score) if best_score >= 0.78 else (model, best_score)


def compact(value: str) -> str:
    return "".join(char.lower() for char in value if char.isalnum())
