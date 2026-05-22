"""从人工整理的跨学科英文学术术语库生成向量库。

arXiv 实时抓取受 API 限流，改为用预定义术语列表，
覆盖物理/材料/CS/生物/工程 5 大学科，约 800 条高质量术语。
"""

import os
import sys
from pathlib import Path

import numpy as np
from sentence_transformers import SentenceTransformer

os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

OUT_TERMS = ROOT / "paperpilot" / "terms_en.npy"
OUT_VECS = ROOT / "paperpilot" / "terms_en_vecs.npy"

TERMS = [
    # ── Physics / Materials ──
    "perovskite", "solar cell", "photovoltaic", "semiconductor", "band gap",
    "optoelectronic", "nanomaterial", "graphene", "carbon nanotube", "quantum dot",
    "superconductor", "ferroelectric", "piezoelectric", "catalyst", "electrode",
    "battery", "supercapacitor", "fuel cell", "thermoelectric", "photocatalyst",
    "thin film", "crystal structure", "density functional theory", "molecular dynamics",
    "monte carlo simulation", "phase transition", "thermal conductivity", "mechanical property",
    "x-ray diffraction", "electron microscopy", "raman spectroscopy",
    "photoluminescence", "electroluminescence", "quantum efficiency", "power conversion efficiency",
    "open circuit voltage", "short circuit current", "charge transport",
    "recombination", "defect passivation", "interface engineering", "device physics",
    "heterojunction", "transistor", "diode", "laser", "light emitting diode",
    "photodetector", "sensor", "energy harvesting", "energy storage",
    "lithium ion battery", "solid state electrolyte", "composite", "nanocomposite",
    "corrosion", "oxidation", "electrocatalysis", "water splitting",
    "hydrogen evolution reaction", "oxygen evolution reaction", "carbon dioxide reduction",
    "metal organic framework", "covalent organic framework", "perovskite oxide",
    "two-dimensional material", "transition metal dichalcogenide", "plasmonic",
    "metamaterial", "photonic crystal", "topological insulator", "quantum computing",
    "quantum information", "superconducting qubit", "ion trap", "quantum entanglement",
    "quantum error correction", "nitrogen vacancy center", "spintronics",
    "magnetic material", "multiferroic", "skyrmion", "exchange bias",
    # ── Computer Science / AI / ML ──
    "machine learning", "deep learning", "neural network", "convolutional neural network",
    "recurrent neural network", "transformer model", "attention mechanism",
    "natural language processing", "computer vision", "reinforcement learning",
    "generative adversarial network", "variational autoencoder", "graph neural network",
    "knowledge graph", "transfer learning", "federated learning", "contrastive learning",
    "self-supervised learning", "representation learning", "large language model",
    "fine-tuning", "in-context learning", "chain of thought", "retrieval augmented generation",
    "word embedding", "tokenization", "multi-head attention", "positional encoding",
    "layer normalization", "batch normalization", "dropout regularization",
    "stochastic gradient descent", "adam optimizer", "learning rate schedule",
    "cross entropy loss", "backpropagation", "overfitting", "ensemble learning",
    "random forest", "support vector machine", "decision tree", "k-means clustering",
    "dimensionality reduction", "principal component analysis", "autoencoder",
    "anomaly detection", "recommendation system", "collaborative filtering",
    "sentiment analysis", "named entity recognition", "machine translation",
    "text summarization", "question answering", "speech recognition",
    "image classification", "object detection", "semantic segmentation",
    "diffusion model", "bayesian inference", "gaussian process",
    "reinforcement learning agent", "q-learning", "policy gradient",
    "multi-agent system", "game theory", "few-shot learning", "zero-shot learning",
    "domain adaptation", "adversarial robustness", "model interpretability",
    "differential privacy", "homomorphic encryption", "federated optimization",
    "blockchain technology", "distributed ledger", "consensus algorithm",
    "edge computing", "cloud computing", "digital twin", "augmented reality",
    "virtual reality", "human computer interaction", "user interface design",
    "software engineering", "agile development", "devops", "continuous integration",
    "microservice architecture", "container orchestration", "kubernetes",
    "api design", "database management", "sql optimization", "nosql database",
    "graph database", "time series database", "data warehouse", "etl pipeline",
    "stream processing", "batch processing", "apache spark", "hadoop",
    "data mining", "association rule mining", "frequent pattern mining",
    "social network analysis", "community detection", "influence maximization",
    "recommender systems", "information retrieval", "search engine",
    "text mining", "web scraping", "data visualization", "business intelligence",
    # ── Biology / Chemistry / Medicine ──
    "gene expression", "protein structure prediction", "cell signaling pathway",
    "metabolic pathway", "genome sequencing", "crispr gene editing", "epigenetics",
    "single cell sequencing", "drug discovery", "drug delivery system",
    "nanoparticle drug carrier", "liposome formulation", "antibody therapy",
    "vaccine development", "cancer immunotherapy", "t cell receptor",
    "cytokine storm", "inflammation pathway", "apoptosis mechanism",
    "autophagy regulation", "oxidative stress", "antioxidant enzyme",
    "stem cell therapy", "organoid culture", "tissue engineering",
    "biomaterial scaffold", "hydrogel synthesis", "biodegradable polymer",
    "enzyme kinetics", "receptor binding", "signal transduction pathway",
    "phosphorylation cascade", "transcription factor binding", "dna repair mechanism",
    "rna interference", "micro rna regulation", "protein folding dynamics",
    "neurodegenerative disease", "alzheimer disease mechanism", "parkinson disease",
    "cardiovascular disease", "metabolic syndrome", "diabetes mellitus",
    "infectious disease", "antiviral therapy", "antibiotic resistance",
    "microbiome analysis", "gut brain axis", "probiotics prebiotics",
    "synthetic biology", "metabolic engineering", "directed evolution",
    "protein engineering", "enzyme design", "biocatalysis", "biosynthesis",
    "natural product synthesis", "total synthesis", "asymmetric synthesis",
    "organocatalysis", "cross coupling reaction", "c-h activation",
    "photoredox catalysis", "electrochemical synthesis", "flow chemistry",
    "click chemistry", "bioconjugation", "peptide synthesis", "oligonucleotide synthesis",
    "polymerization", "living polymerization", "ring opening polymerization",
    "supramolecular chemistry", "host guest chemistry", "molecular recognition",
    "self-assembly", "dna origami", "molecular machine", "rotaxane",
    "coordination chemistry", "organometallic catalysis", "zeolite catalysis",
    # ── Engineering ──
    "control system design", "pid controller", "adaptive control",
    "robust control", "model predictive control", "kalman filtering",
    "signal processing", "fourier transform analysis", "wavelet transform",
    "image segmentation", "feature extraction algorithm", "pattern recognition system",
    "robotic manipulation", "kinematic modeling", "trajectory optimization",
    "simultaneous localization and mapping", "sensor fusion technique",
    "finite element analysis", "computational fluid dynamics",
    "heat transfer analysis", "turbulence modeling", "aerodynamic design",
    "structural optimization", "vibration analysis", "fatigue life prediction",
    "fracture mechanics analysis", "tribological coating", "renewable energy system",
    "wind turbine design", "solar thermal collector", "smart grid technology",
    "power electronic converter", "integrated circuit design",
    "embedded system design", "real-time operating system",
    "parallel computing", "distributed computing system",
    "internet of things device", "wireless communication", "antenna design",
    "mimo system", "beamforming algorithm", "channel coding",
    "network security", "intrusion detection system", "firewall configuration",
    "cryptographic protocol", "public key infrastructure", "digital signature",
    "authentication mechanism", "access control policy", "zero trust architecture",
    "software defined networking", "network function virtualization",
    "quality of service", "congestion control", "routing protocol",
    "load balancing", "fault tolerance", "disaster recovery",
    "backup strategy", "high availability", "scalability design",
    "performance optimization", "benchmarking methodology", "capacity planning",
    "resource allocation", "scheduling algorithm", "workflow management",
    "supply chain optimization", "logistics planning", "inventory management",
    "manufacturing process", "additive manufacturing", "3d printing technology",
    "subtractive manufacturing", "cnc machining", "laser cutting",
    "welding technology", "surface treatment", "quality control",
    "statistical process control", "six sigma methodology", "lean manufacturing",
    "industrial automation", "programmable logic controller", "supervisory control",
    "distributed control system", "human machine interface", "industrial internet",
    "industry 4.0", "cyber-physical system", "digital manufacturing",
    "electric vehicle", "autonomous driving", "lidar sensor",
    "battery management system", "electric motor control", "power train",
    "hybrid electric vehicle", "fuel cell vehicle", "charging infrastructure",
    "vehicle to grid", "intelligent transportation", "traffic flow optimization",
    "structural health monitoring", "nondestructive testing", "acoustic emission",
    "ultrasonic testing", "thermography inspection", "structural dynamics",
    "seismic analysis", "earthquake engineering", "wind engineering",
    "bridge engineering", "geotechnical engineering", "foundation design",
    "slope stability analysis", "tunnel engineering", "hydraulic engineering",
    "water resource management", "wastewater treatment", "air pollution control",
    "environmental monitoring", "climate modeling", "carbon capture",
    "carbon sequestration", "life cycle assessment", "sustainability metrics",
]


def main():
    print(f"Terms: {len(TERMS)}")

    print("Loading multilingual model ...")
    model_path = str(Path.home() / ".cache/modelscope/sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
    if not Path(model_path).exists():
        model_path = "paraphrase-multilingual-MiniLM-L12-v2"
    model = SentenceTransformer(model_path)

    print("Embedding terms ...")
    terms_arr = np.array(sorted(set(TERMS)), dtype=str)
    vecs = model.encode(terms_arr.tolist(), normalize_embeddings=True, show_progress_bar=True)

    print(f"Saving: {terms_arr.shape}, {vecs.shape}")
    np.save(str(OUT_TERMS), terms_arr)
    np.save(str(OUT_VECS), vecs.astype(np.float32))
    print(f"Done: {OUT_TERMS.name} ({len(terms_arr)} terms), {OUT_VECS.name} ({vecs.nbytes / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
