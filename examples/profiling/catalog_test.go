package catalog

import (
	"math"
	"math/rand"
	"runtime"
	"sort"
	"strings"
	"sync"
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
	id            int
	region        string
	stock         int
	featured      bool
	words         []int
	lengthPenalty float64
}

type posting struct {
	doc        int
	frequency  int
	titleCount int
}

type term struct {
	docs   []posting
	weight float64
}

type catalog struct {
	entries    []entry
	vocabulary map[string]int
	terms      []term
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
	index := catalog{
		entries:    make([]entry, 0, len(listings)),
		vocabulary: make(map[string]int),
	}
	seen := make([]posting, 0)
	touched := make([]int, 0)
	totalLength := 0
	for doc, item := range listings {
		title, details := tokenize(item.title), tokenize(item.details)
		wordIDs := make([]int, 0, len(title)+len(details))
		touched = touched[:0]
		for part, words := range [2][]string{title, details} {
			for _, word := range words {
				id, ok := index.vocabulary[word]
				if !ok {
					id = len(index.terms)
					index.vocabulary[word] = id
					index.terms = append(index.terms, term{})
					seen = append(seen, posting{doc: -1})
				}
				if seen[id].doc != doc {
					seen[id] = posting{doc: doc}
					touched = append(touched, id)
				}
				seen[id].frequency++
				if part == 0 {
					seen[id].titleCount++
				}
				wordIDs = append(wordIDs, id)
			}
		}
		for _, id := range touched {
			index.terms[id].docs = append(index.terms[id].docs, seen[id])
		}
		index.entries = append(index.entries, entry{
			id: item.id, region: item.region, stock: item.stock, featured: item.featured,
			words: wordIDs,
		})
		totalLength += len(wordIDs)
	}
	if len(listings) == 0 {
		return index
	}
	meanLength := math.Max(1, float64(totalLength)/float64(len(listings)))
	for i := range index.entries {
		item := &index.entries[i]
		length := float64(len(item.words)) / meanLength
		item.lengthPenalty = 1.2 * (0.25 + 0.75*length)
	}
	for i := range index.terms {
		item := &index.terms[i]
		n := float64(len(item.docs))
		item.weight = math.Log1p((float64(len(listings)) - n + 0.5) / (n + 0.5))
	}
	return index
}

func containsPhrase(words, phrase []int) bool {
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

func ranksBefore(left, right match) bool {
	if left.score == right.score {
		return left.id < right.id
	}
	return left.score > right.score
}

func insertMatch(matches []match, item match, limit int) []match {
	if limit == 0 || (len(matches) == limit && !ranksBefore(item, matches[len(matches)-1])) {
		return matches
	}
	position := sort.Search(len(matches), func(i int) bool { return ranksBefore(item, matches[i]) })
	if len(matches) < limit {
		matches = append(matches, match{})
	}
	copy(matches[position+1:], matches[position:])
	matches[position] = item
	return matches
}

func (index catalog) search(query request) response {
	result := response{regions: make(map[string]int)}
	words := tokenize(query.text)
	terms := make([]*term, 0, len(words))
	shortest := 0
	for _, word := range words {
		id, ok := index.vocabulary[word]
		if !ok {
			return result
		}
		if len(terms) > 0 && len(index.terms[id].docs) < len(terms[shortest].docs) {
			shortest = len(terms)
		}
		terms = append(terms, &index.terms[id])
	}
	phraseWords := tokenize(query.phrase)
	phrase := make([]int, 0, len(phraseWords))
	for _, word := range phraseWords {
		id, ok := index.vocabulary[word]
		if !ok {
			return result
		}
		phrase = append(phrase, id)
	}

	consider := func(doc int, positions []int) {
		item := index.entries[doc]
		if (query.available && item.stock == 0) ||
			!containsPhrase(item.words, phrase) {
			return
		}
		result.regions[item.region]++
		if query.region != "" && item.region != query.region {
			return
		}
		score := 0.0
		for i, term := range terms {
			hit := term.docs[positions[i]]
			frequency := float64(hit.frequency)
			score += term.weight * (2.2*frequency/(frequency+item.lengthPenalty) +
				0.4*float64(hit.titleCount))
		}
		if item.featured {
			score *= 1.05
		}
		result.matches = insertMatch(result.matches, match{item.id, score}, query.limit)
	}

	if len(terms) == 0 {
		for doc := range index.entries {
			consider(doc, nil)
		}
		return result
	}
	positions := make([]int, len(terms))
	// Posting lists are sorted by entry number, so cursors only move forward.
	for i, hit := range terms[shortest].docs {
		positions[shortest] = i
		matched := true
		for j, list := range terms {
			if j == shortest {
				continue
			}
			for positions[j] < len(list.docs) && list.docs[positions[j]].doc < hit.doc {
				positions[j]++
			}
			if positions[j] == len(list.docs) {
				return result
			}
			if list.docs[positions[j]].doc != hit.doc {
				matched = false
				break
			}
		}
		if matched {
			consider(hit.doc, positions)
		}
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
	slots := make(map[request]int, len(queries))
	unique := make([]request, 0, len(queries))
	for _, query := range queries {
		if _, ok := slots[query]; !ok {
			slots[query] = len(unique)
			unique = append(unique, query)
		}
	}

	results := make([]response, len(unique))
	workers := min(len(unique), runtime.GOMAXPROCS(0))
	if workers < 2 || len(index.entries) < 4096 {
		for i, query := range unique {
			results[i] = index.search(query)
		}
	} else {
		var group sync.WaitGroup
		for worker := 0; worker < workers; worker++ {
			group.Add(1)
			go func(start int) {
				defer group.Done()
				for i := start; i < len(unique); i += workers {
					results[i] = index.search(unique[i])
				}
			}(worker)
		}
		group.Wait()
	}

	var digest uint64
	for _, query := range queries {
		result := results[slots[query]]
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
