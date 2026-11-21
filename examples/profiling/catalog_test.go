package catalog

import (
	"math"
	"math/rand"
	"sort"
	"strings"
	"testing"
	"unicode"
)

type listing struct {
	id       int
	title    string
	details  string
	region   string
	stock    int
	featured bool
}

type entry struct {
	listing
	titleWords []string
	words      []string
	counts     map[string]int
}

type catalog struct {
	entries           []entry
	documentFrequency map[string]int
	meanLength        float64
}

type request struct {
	text      string
	phrase    string
	region    string
	available bool
	limit     int
}

type match struct {
	id    int
	score float64
}

type response struct {
	matches []match
	regions map[string]int
}

func tokenize(text string) []string {
	return strings.FieldsFunc(strings.ToLower(text), func(r rune) bool {
		return !unicode.IsLetter(r) && !unicode.IsDigit(r)
	})
}

func buildCatalog(listings []listing) catalog {
	index := catalog{documentFrequency: make(map[string]int)}
	totalLength := 0
	for _, item := range listings {
		title := tokenize(item.title)
		words := tokenize(item.title + " " + item.details)
		tally := make(map[string]int)
		for _, word := range words {
			tally[word]++
		}
		for word := range tally {
			index.documentFrequency[word]++
		}
		index.entries = append(index.entries, entry{item, title, words, tally})
		totalLength += len(words)
	}
	if len(listings) != 0 {
		index.meanLength = math.Max(1, float64(totalLength)/float64(len(listings)))
	}
	return index
}

func countWord(words []string, target string) int {
	count := 0
	for _, word := range words {
		if word == target {
			count++
		}
	}
	return count
}

func containsPhrase(words, phrase []string) bool {
	if len(phrase) == 0 {
		return true
	}
	for start := 0; start+len(phrase) <= len(words); start++ {
		i := 0
		for i < len(phrase) && words[start+i] == phrase[i] {
			i++
		}
		if i == len(phrase) {
			return true
		}
	}
	return false
}

func (index catalog) rank(item entry, words []string) (float64, bool) {
	score := 0.0
	for _, word := range words {
		count := item.counts[word]
		if count == 0 {
			return 0, false
		}
		n := float64(index.documentFrequency[word])
		weight := math.Log1p((float64(len(index.entries)) - n + 0.5) / (n + 0.5))
		frequency := float64(count)
		length := float64(len(item.words)) / index.meanLength
		score += weight * (2.2*frequency/(frequency+1.2*(0.25+0.75*length)) +
			0.4*float64(countWord(item.titleWords, word)))
	}
	if item.featured {
		score *= 1.05
	}
	return score, true
}

func (index catalog) search(query request) response {
	terms := tokenize(query.text)
	phrase := tokenize(query.phrase)
	result := response{regions: make(map[string]int)}
	for _, item := range index.entries {
		if query.available && item.stock == 0 {
			continue
		}
		score, ok := index.rank(item, terms)
		if !ok || !containsPhrase(item.words, phrase) {
			continue
		}
		result.regions[item.region]++
		if query.region == "" || item.region == query.region {
			result.matches = append(result.matches, match{item.id, score})
		}
	}
	sort.Slice(result.matches, func(i, j int) bool {
		left, right := result.matches[i], result.matches[j]
		if left.score == right.score {
			return left.id < right.id
		}
		return left.score > right.score
	})
	if len(result.matches) > query.limit {
		result.matches = result.matches[:query.limit]
	}
	return result
}

func makeListings(count int) []listing {
	rng := rand.New(rand.NewSource(54931))
	regions := []string{"north", "south", "east", "west"}
	categories := []string{"camp stove", "water bottle", "trail pack", "field lantern",
		"climbing rope", "sleeping mat", "rain jacket", "repair kit"}
	descriptions := []string{
		"all weather", "compact folding", "heavy duty", "quick release",
		"field tested", "low profile", "machine washable", "recycled material",
		"adjustable strap", "limited edition", "includes accessories", "easy clean",
	}
	items := make([]listing, count)
	for i := range items {
		category := categories[rng.Intn(len(categories))]
		finish := descriptions[rng.Intn(len(descriptions))]
		var details strings.Builder
		for n := 8 + rng.Intn(16); n > 0; n-- {
			details.WriteString(descriptions[rng.Intn(len(descriptions))])
			details.WriteString("; ")
		}
		items[i] = listing{
			id: i + 1, title: category + " / " + finish, details: details.String(),
			region: regions[rng.Intn(len(regions))], stock: rng.Intn(8), featured: rng.Intn(12) == 0,
		}
	}
	return items
}

func searchBatch(index catalog, queries []request) uint64 {
	var digest uint64
	for _, query := range queries {
		result := index.search(query)
		for _, item := range result.matches {
			digest = digest*1099511628211 ^ uint64(item.id)
		}
		for _, region := range [...]string{"north", "south", "east", "west"} {
			digest = digest*1099511628211 ^ uint64(result.regions[region])
		}
	}
	return digest
}

func TestCatalogSearch(t *testing.T) {
	index := buildCatalog([]listing{
		{id: 1, title: "Trail pack", details: "All-weather trail gear", region: "west", stock: 1},
		{id: 2, title: "Trail pack", details: "All-weather trail gear", region: "north", stock: 1},
		{id: 3, title: "Trail pack", details: "All-weather trail gear", region: "west"},
		{id: 4, title: "Trail pack", details: "All gear for wet weather", region: "west", stock: 1},
	})
	got := index.search(request{text: "TRAIL pack", phrase: "all weather", region: "west", available: true, limit: 5})
	if len(got.matches) != 1 || got.matches[0].id != 1 ||
		got.regions["west"] != 1 || got.regions["north"] != 1 {
		t.Fatalf("unexpected search response: %+v", got)
	}
}

var measuredCatalog catalog
var measuredDigest uint64

func BenchmarkCatalog(b *testing.B) {
	for _, size := range []struct {
		name     string
		listings int
	}{{"Small", 1024}, {"Large", 8192}} {
		b.Run(size.name, func(b *testing.B) {
			items := makeListings(size.listings)
			b.Run("Index", func(b *testing.B) {
				b.ReportAllocs()
				for i := 0; i < b.N; i++ {
					measuredCatalog = buildCatalog(items)
				}
			})
			index := buildCatalog(items)
			requests := []request{
				{text: "trail pack", phrase: "all weather", region: "west", available: true, limit: 12},
				{text: "folding", available: true, limit: 20},
				{text: "water bottle", phrase: "easy clean", limit: 8},
				{text: "field lantern", region: "north", limit: 12},
				{text: "low profile", available: true, limit: 15},
				{text: "climbing rope", region: "east", available: true, limit: 8},
				{text: "folding", available: true, limit: 20},
				{text: "camp stove", phrase: "includes accessories", limit: 12},
			}
			b.Run("Search", func(b *testing.B) {
				b.ReportAllocs()
				for i := 0; i < b.N; i++ {
					measuredDigest = searchBatch(index, requests)
				}
			})
		})
	}
}
